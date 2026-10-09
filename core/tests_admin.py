"""
core/tests_admin.py

Admin performance guard: every admin page must cost the same number of queries
however much data there is. A page whose query count grows with the table size
(an N+1 in list_display, a __str__ that follows a foreign key inside a <select>
listing every product, a per-row .count()) is fine on a dev database and takes
seconds in production — which is how the admin got slow in the first place.

The test renders every registered changelist and the change page of the
heavy models, doubles the data, renders again, and fails on any page whose
query count went up.

Run: DB_HOST=localhost python manage.py test core.tests_admin --keepdb
"""

from decimal import Decimal

from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from ecommerce.celery import app as celery_app

celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = False

from notification.models import ContactInquiry, Notification, SupportTicket
from order.models import Cart, CartItem, Order, OrderProduct, Shipment, TrackingEvent
from product.models import (
    Brand, Category, Collection, Color, DeliveryOption, Main_Category, Product,
    ProductDeliveryOption, ProductReview, ProductViewLog, Size, Sub_Category,
    Variants, Wishlist,
)
from userauths.models import User
from vendor.models import Vendor, VendorActivityLog

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}

#: Models whose change page is rendered too (the ones with big FK dropdowns or inlines).
CHANGE_PAGES = [
    Product, Variants, Vendor, Order, Shipment, ProductReview, Wishlist, CartItem,
    ProductDeliveryOption, Sub_Category, Collection, OrderProduct, Notification,
    SupportTicket,
]


def _user(tag, **kwargs):
    return User.objects.create(
        email=f"{tag}@example.com", first_name=tag, last_name='T',
        phone=f"+233{abs(hash(tag)) % 10**9:09d}", **kwargs,
    )


def build_unit(n):
    """One slice of a realistic store: a seller, a category branch, products with
    variants, an order with a shipment, a review, notifications, logs."""
    seller = _user(f"seller{n}")
    vendor = Vendor.objects.create(
        name=f"Shop {n}", user=seller, email=f"shop{n}@example.com",
        contact=f"+23320{n:07d}", is_approved=True, status='VERIFIED',
    )
    main = Main_Category.objects.create(title=f"Main {n}")
    category = Category.objects.create(title=f"Cat {n}", main_category=main)
    sub = Sub_Category.objects.create(title=f"Sub {n}", category=category)
    brand = Brand.objects.create(title=f"Brand {n}")
    color = Color.objects.create(name=f"Color {n}", code='#000000')
    size = Size.objects.create(name=f"Size {n}", code=f"S{n}")
    delivery = DeliveryOption.objects.create(name=f"Courier {n}", description='x', min_days=1, max_days=3)

    shopper = _user(f"shopper{n}")
    products = []
    for p in range(3):
        product = Product.objects.create(
            title=f"Product {n}-{p}", sub_category=sub, vendor=vendor, brand=brand,
            status='published', price=Decimal('100'), old_price=Decimal('120'), total_quantity=5,
        )
        products.append(product)
        for _ in range(2):
            variant = Variants.objects.create(product=product, color=color, size=size, quantity=3, price=Decimal('100'))
            ProductDeliveryOption.objects.create(product=product, variant=variant, delivery_option=delivery)
        ProductViewLog.objects.create(product=product, visitor_key=f"v{n}", is_bot=False,
                                      is_returning=False, device_type='desktop', date=timezone.now().date())
        Wishlist.objects.create(user=shopper, product=product)

    collection = Collection.objects.create(title=f"Collection {n}", slug=f"collection-{n}")
    collection.products.add(*products)

    cart = Cart.objects.create(user=shopper)
    CartItem.objects.create(cart=cart, product=products[0], quantity=1)

    order = Order.objects.create(user=shopper, total=Decimal('200'), is_ordered=True, status='processing')
    order.vendors.add(vendor)
    items = [OrderProduct.objects.create(order=order, product=pr, quantity=1, price=pr.price, amount=pr.price)
             for pr in products[:2]]
    shipment = Shipment.objects.create(order=order, vendor=vendor)
    shipment.items.add(*items)
    TrackingEvent.objects.create(shipment=shipment, status='in_transit', description='Moving', event_date=timezone.now())

    ProductReview.objects.create(user=shopper, product=products[0], vendor=vendor, review='Good', rating=5,
                                 moderation_status=ProductReview.APPROVED)
    Notification.objects.create(
        recipient=seller, verb='vendor_new_order',
        actor_content_type=ContentType.objects.get_for_model(User), actor_object_id=shopper.pk,
        target_content_type=ContentType.objects.get_for_model(Order), target_object_id=order.pk,
    )
    VendorActivityLog.objects.create(vendor=vendor, event_type='login')
    inquiry = ContactInquiry.objects.create(name='Ama', email=f"ama{n}@example.com", subject='Help', message='Hi')
    SupportTicket.objects.create(inquiry=inquiry, assigned_to=None)


@override_settings(**TEST_SETTINGS)
class AdminQueryCountTests(TestCase):

    def setUp(self):
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1.0, 'USD': 0.094, 'EUR': 0.087}, 3600)
        self.staff = _user('admin', is_staff=True, is_superuser=True, is_active=True)
        self.client.force_login(self.staff)

    def _measure(self):
        counts = {}
        for model, model_admin in admin.site._registry.items():
            meta = model._meta
            pages = [('list', reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist"))]
            if model in CHANGE_PAGES:
                obj = model._default_manager.order_by('pk').first()
                if obj is not None:
                    pages.append(('change', reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[obj.pk])))
            for kind, url in pages:
                with CaptureQueriesContext(connection) as ctx:
                    response = self.client.get(url)
                # 302: singleton admins (e.g. email config) redirect the changelist to their one object.
                self.assertIn(response.status_code, (200, 302), f"{url} → {response.status_code}")
                counts[f"{meta.label} {kind}"] = len(ctx.captured_queries)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(reverse('admin:index')).status_code, 200)
        counts['admin index'] = len(ctx.captured_queries)
        return counts

    def test_query_count_does_not_grow_with_data(self):
        for n in range(3):
            build_unit(n)
        small = self._measure()
        for n in range(3, 9):
            build_unit(n)
        large = self._measure()

        report = sorted(((large[k] - small[k], k, small[k], large[k]) for k in small), reverse=True)
        print("\n  growth  page  (3 units -> 9 units)")
        for growth, page, a, b in sorted(report, key=lambda r: -r[3])[:15]:
            print(f"  {growth:+5d}  {page}: {a} -> {b}")

        grew = [f"{page}: {a} -> {b}" for growth, page, a, b in report if growth > 0]
        self.assertFalse(grew, "Admin pages whose query count grows with the data:\n  " + "\n  ".join(grew))


@override_settings(**TEST_SETTINGS)
class AdminDashboardTests(TestCase):

    def setUp(self):
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1.0, 'USD': 0.094, 'EUR': 0.087}, 3600)
        for n in range(2):
            build_unit(n)  # each unit: one processing order of GHS 200, 2 items of GHS 100

    def test_superuser_sees_sales_figures(self):
        from core.admin_dashboard import dashboard_for
        admin_user = _user('boss', is_staff=True, is_superuser=True, is_active=True)
        dash = dashboard_for(admin_user, refresh=True)

        self.assertEqual(dash['kpis']['orders_30'], 2)
        self.assertEqual(dash['kpis']['revenue_30'], 400.0)
        self.assertEqual(dash['kpis']['revenue_today'], 400.0)
        self.assertEqual(sum(dash['chart']['revenue']), 400.0)
        self.assertEqual(len(dash['chart']['labels']), 30)
        self.assertEqual(len(dash['top_products']), 4)
        self.assertEqual({s['label']: s['count'] for s in dash['order_status']}['Processing'], 2)
        self.assertEqual(len(dash['recent_orders']), 2)

        self.client.force_login(admin_user)
        response = self.client.get(reverse('admin:index'))
        self.assertContains(response, 'Needs attention')
        self.assertContains(response, 'nm-sales-chart')

    def test_cancelled_orders_are_not_revenue(self):
        from core.admin_dashboard import dashboard_for
        Order.objects.filter(pk=Order.objects.order_by('pk').first().pk).update(status='canceled')
        dash = dashboard_for(_user('boss', is_staff=True, is_superuser=True, is_active=True), refresh=True)
        self.assertEqual(dash['kpis']['revenue_30'], 200.0)

    def test_staff_without_order_permission_sees_no_sales(self):
        from django.contrib.auth.models import Permission
        from core.admin_dashboard import dashboard_for
        staff = _user('helper', is_staff=True, is_active=True)
        staff.user_permissions.add(Permission.objects.get(codename='view_productreview'))
        staff = User.objects.get(pk=staff.pk)  # reset the permission cache
        dash = dashboard_for(staff, refresh=True)

        self.assertIsNone(dash['kpis'])
        self.assertEqual(dash['recent_orders'], [])
        self.assertEqual([a['perm'] for a in dash['attention']], ['product.view_productreview'])

        self.client.force_login(staff)
        response = self.client.get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Revenue today')


@override_settings(**TEST_SETTINGS)
class AdminEdgeCaseTests(TestCase):
    """Data shapes that used to crash admin pages, and bulk actions run on the
    annotated changelist querysets."""

    def setUp(self):
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1.0, 'USD': 0.094, 'EUR': 0.087}, 3600)
        build_unit(0)
        self.staff = _user('admin', is_staff=True, is_superuser=True, is_active=True)
        self.client.force_login(self.staff)

    def _get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response

    def test_guest_cart_and_deleted_product_render(self):
        guest_cart = Cart.objects.create(user=None, session_id='guest-1') if hasattr(Cart, 'session_id') \
            else Cart.objects.create(user=None)
        orphan = CartItem.objects.create(cart=guest_cart, product=Product.objects.first(), quantity=2)
        CartItem.objects.filter(pk=orphan.pk).update(product=None, variant=None)

        self.assertTrue(str(guest_cart).startswith('Guest cart'))
        self._get(reverse('admin:order_cartitem_changelist'))
        self._get(reverse('admin:order_cart_changelist'))
        self._get(reverse('admin:order_cart_change', args=[guest_cart.pk]))
        self._get(reverse('admin:order_cartitem_change', args=[orphan.pk]))

    def test_category_without_parent_renders(self):
        Category.objects.update(main_category=None)
        Sub_Category.objects.create(title='Orphan', category=None)
        self.assertEqual(str(Category.objects.first()), Category.objects.first().title)
        self._get(reverse('admin:product_category_changelist'))
        self._get(reverse('admin:product_sub_category_changelist'))
        self._get(reverse('admin:product_product_change', args=[Product.objects.first().pk]))

    def test_notification_pointing_at_unregistered_model(self):
        from django.contrib.auth.models import Permission
        permission = Permission.objects.first()  # auth.Permission has no admin
        Notification.objects.create(
            recipient=self.staff, verb='announcement',
            target_content_type=ContentType.objects.get_for_model(Permission), target_object_id=permission.pk,
        )
        self.assertContains(self._get(reverse('admin:notification_notification_changelist')), str(permission))

    def _action(self, model, action, pks, **extra):
        meta = model._meta
        url = reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")
        return self.client.post(url, {'action': action, '_selected_action': pks, **extra})

    def test_status_actions_on_annotated_querysets(self):
        shipment = Shipment.objects.first()
        self.assertEqual(self._action(Shipment, 'mark_delivered', [shipment.pk]).status_code, 302)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'delivered')

        order = Order.objects.first()
        self.assertEqual(self._action(Order, 'mark_canceled', [order.pk]).status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'canceled')

    def test_delete_action_on_annotated_querysets(self):
        brand = Brand.objects.create(title='Throwaway brand')
        sub = Sub_Category.objects.create(title='Throwaway sub', category=Category.objects.first())
        for model, obj in [(Brand, brand), (Sub_Category, sub), (Shipment, Shipment.objects.first())]:
            confirm = self._action(model, 'delete_selected', [obj.pk])
            self.assertEqual(confirm.status_code, 200, model)  # confirmation page
            done = self._action(model, 'delete_selected', [obj.pk], post='yes')
            self.assertEqual(done.status_code, 302, model)
            self.assertFalse(model.objects.filter(pk=obj.pk).exists(), model)

    def test_dashboard_links_are_valid_filters(self):
        from core.admin_dashboard import dashboard_for
        dash = dashboard_for(self.staff, refresh=True)
        urls = [a['url'] for a in dash['attention']] + [s['url'] for s in dash['order_status']]
        urls += list(dash['links'].values())
        for url in urls:
            response = self.client.get(url)
            # The admin answers an invalid filter with a redirect to ?e=1.
            self.assertEqual(response.status_code, 200, url)

    def test_dashboard_counts_match_linked_lists(self):
        from core.admin_dashboard import dashboard_for
        Variants.objects.update(quantity=1)
        dash = dashboard_for(self.staff, refresh=True)
        for item in dash['attention']:
            response = self.client.get(item['url'])
            if 'Processing 48h+' in item['label']:
                continue  # the list can't filter on "has no shipment"; it shows a superset
            self.assertEqual(response.context['cl'].result_count, item['count'], item['label'])


@override_settings(**TEST_SETTINGS)
class SupportTicketTests(TestCase):

    def setUp(self):
        from django.core import mail
        self.mail = mail
        cache.clear()
        self.staff = _user('agent', is_staff=True, is_superuser=True, is_active=True)
        self.client.force_login(self.staff)

    def _inquiry(self, n):
        return ContactInquiry.objects.create(name='Ama', email=f"ama{n}@example.com", subject='Help', message='Hi')

    def test_ticket_ids_stay_unique_after_a_delete(self):
        first = SupportTicket.objects.create(inquiry=self._inquiry(1))
        second = SupportTicket.objects.create(inquiry=self._inquiry(2))
        first.delete()
        third = SupportTicket.objects.create(inquiry=self._inquiry(3))  # count()+1 collided with `second`
        self.assertNotEqual(third.ticket_id, second.ticket_id)

    def test_reply_view_creates_ticket_and_emails_once(self):
        from notification.views import send_reply_view
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        inquiry = self._inquiry(9)  # no ticket yet
        request = RequestFactory().post('/x/', {'reply_message': 'We are on it', 'internal_note': 'VIP'})
        request.user = self.staff
        request.session = self.client.session
        request._messages = FallbackStorage(request)

        response = send_reply_view(request, inquiry.pk)

        self.assertEqual(response.status_code, 302)
        ticket = SupportTicket.objects.get(inquiry=inquiry)
        self.assertEqual(ticket.replies.count(), 2)
        self.assertEqual(len(self.mail.outbox), 1)  # the signal's email only
        self.assertEqual(self.mail.outbox[0].to, [inquiry.email])

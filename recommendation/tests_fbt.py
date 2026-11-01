"""
"Frequently bought together": seller pins first, strict co-purchase, featured-offer swap, stock filter.

Run with:  DB_HOST=localhost python manage.py test recommendation.tests_fbt --keepdb
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from product.models import Brand, CatalogItem, Category, Main_Category, Product, Sub_Category
from recommendation.models import KIND_CO_PURCHASE, KIND_HYBRID, PinnedComplement, ProductNeighbor
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


@override_settings(**TEST_SETTINGS)
class FbtTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owner = User.objects.create_user('Fbt', 'Owner', 'fbt1@example.com', '+233252000001', 'Str0ng!Passw0rd')
        User.objects.filter(pk=self.owner.pk).update(is_active=True, email_verified_at=timezone.now())
        self.vendor = Vendor.objects.create(name='Fbt Shop', user=self.owner, email='fbt-shop@example.com',
                                            contact='+233550004444', is_approved=True, status='VERIFIED')
        main = Main_Category.objects.create(title='Fbt Main')
        sub = Sub_Category.objects.create(title='Fbt Sub', category=Category.objects.create(title='Fbt Cat', main_category=main))
        brand = Brand.objects.create(title='Fbt Brand')

        def make(title, price, qty=5, vendor=None):
            return Product.objects.create(title=title, sub_category=sub, vendor=vendor or self.vendor, status='published',
                                          price=Decimal(price), total_quantity=qty, brand=brand)
        self.make = make
        self.phone = make('Fbt Phone', '1000')
        self.case = make('Fbt Case', '50')
        self.charger = make('Fbt Charger', '80')
        self.other_phone = make('Fbt Other Phone', '900')

    def neighbor(self, src, dst, kind=KIND_CO_PURCHASE, rank=0):
        ProductNeighbor.objects.create(product=src, neighbor=dst, kind=kind, rank=rank, score=1.0, support=3)

    def get(self):
        return APIClient().get(f'/api/v1/recommendations/frequently-bought/{self.phone.pk}/').data

    def test_co_purchase_only_no_lookalikes(self):
        self.neighbor(self.phone, self.other_phone, kind=KIND_HYBRID)
        self.assertEqual(self.get()['items'], [])
        from django.core.cache import cache
        # the empty pick list was cached; the nightly retrain changes the version key in production
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        self.neighbor(self.phone, self.case)
        data = self.get()
        self.assertEqual([c['id'] for c in data['items']], [self.phone.pk, self.case.pk])
        self.assertEqual(Decimal(str(data['total'])), Decimal('1050.00'))

    def test_seller_pins_come_first_and_out_of_stock_dropped(self):
        self.neighbor(self.phone, self.case, rank=0)
        PinnedComplement.objects.create(product=self.phone, other=self.charger)
        self.assertEqual([c['id'] for c in self.get()['items']][1:], [self.charger.pk, self.case.pk])
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        Product.objects.filter(pk=self.charger.pk).update(total_quantity=0)
        self.assertEqual([c['id'] for c in self.get()['items']][1:], [self.case.pk])

    def test_grouped_item_shows_featured_offer(self):
        other_owner = User.objects.create_user('Fbt', 'Two', 'fbt2@example.com', '+233252000002', 'Str0ng!Passw0rd')
        shop2 = Vendor.objects.create(name='Fbt Shop 2', user=other_owner, email='fbt-shop2@example.com',
                                      contact='+233550004445', is_approved=True, status='VERIFIED')
        cheaper_case = self.make('Fbt Case', '40', vendor=shop2)
        item = CatalogItem.objects.create(title='Fbt Case')
        Product.objects.filter(pk__in=[self.case.pk, cheaper_case.pk]).update(catalog_item=item)
        Product.objects.filter(pk=self.case.pk).update(is_buybox_winner=False)
        self.neighbor(self.phone, self.case)
        self.assertEqual([c['id'] for c in self.get()['items']][1:], [cheaper_case.pk])

    def test_seller_pins_own_products_only(self):
        api = APIClient()
        api.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        api.credentials(HTTP_X_USER_TYPE='vendor')
        url = f'/api/v1/recommendations/seller/pinned/{self.phone.pk}/'
        res = api.put(url, {'others': [self.case.pk, self.charger.pk]}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([o['id'] for o in res.data['others']], [self.case.pk, self.charger.pk])
        stranger_owner = User.objects.create_user('Fbt', 'Three', 'fbt3@example.com', '+233252000003', 'Str0ng!Passw0rd')
        shop3 = Vendor.objects.create(name='Fbt Shop 3', user=stranger_owner, email='fbt-shop3@example.com',
                                      contact='+233550004446', is_approved=True, status='VERIFIED')
        foreign = self.make('Fbt Foreign', '10', vendor=shop3)
        self.assertEqual(api.put(url, {'others': [foreign.pk]}, format='json').status_code, 400)


@override_settings(**TEST_SETTINGS)
class BuyAgainTests(FbtTests):

    def test_buy_again_ranks_repeat_purchases_and_skips_unavailable(self):
        from datetime import timedelta

        from order.models import Order, OrderProduct
        shopper = User.objects.create_user('Buy', 'Again', 'fbt9@example.com', '+233252000009', 'Str0ng!Passw0rd')
        User.objects.filter(pk=shopper.pk).update(is_active=True)

        def bought(product, days_ago):
            o = Order.objects.create(user=shopper, total=product.price, is_ordered=True, order_number=f'ORD-BA-{Order.objects.count()}')
            Order.objects.filter(pk=o.pk).update(date_created=timezone.now() - timedelta(days=days_ago))
            OrderProduct.objects.create(order=o, product=product, quantity=1, price=product.price, amount=product.price)

        bought(self.phone, 200)
        bought(self.case, 40)
        bought(self.case, 10)
        bought(self.charger, 5)
        Product.objects.filter(pk=self.charger.pk).update(total_quantity=0)

        api = APIClient()
        api.force_authenticate(shopper)
        res = api.get('/api/v1/recommendations/buy-again/')
        self.assertEqual(res.status_code, 200)
        ids = [c['id'] for c in res.data['results']]
        self.assertEqual(ids, [self.case.pk, self.phone.pk], "repeat buy first; out of stock left out")
        self.assertEqual(res.data['results'][0]['times_bought'], 2)
        self.assertEqual(APIClient().get('/api/v1/recommendations/buy-again/').status_code, 401)

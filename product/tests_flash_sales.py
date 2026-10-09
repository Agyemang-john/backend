"""
product/tests_flash_sales.py
Flash sales: live pricing in the cart, max_quantity caps, orders recording
the flash price, the admin form, and sellers managing their own sales.

Run with:  DB_HOST=localhost python manage.py test product.tests_flash_sales --keepdb
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from address.models import Address
from ecommerce.celery import app as celery_app
from order.models import Cart, CartItem, OrderProduct
from order.pricing import line_pricing
from payments.models import SubscriptionPlan, VendorSubscription
from product.admin import FlashSaleAdminForm
from product.models import Brand, Category, FlashSale, Main_Category, Product, Sub_Category, Variants
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}
PASSWORD = 'Str0ng!Passw0rd'


def make_user(n):
    user = User.objects.create_user(f'Flash{n}', 'Test', f'flash{n}@example.com', f'+23324100{n:04d}', PASSWORD)
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class FlashSaleTestCase(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(
            name='Flash Shop', user=self.owner, email='flash-shop@example.com', contact='+233550003333',
            is_approved=True, status='VERIFIED',
        )
        main = Main_Category.objects.create(title='Flash Main')
        cat = Category.objects.create(title='Flash Cat', main_category=main)
        self.sub = Sub_Category.objects.create(title='Flash Sub', category=cat)
        self.brand = Brand.objects.create(title='Flash Brand')
        self.product = Product.objects.create(
            title='Flash Kettle', sub_category=self.sub, vendor=self.vendor, status='published',
            price=Decimal('100.00'), total_quantity=50, brand=self.brand,
        )
        self.red = Variants.objects.create(product=self.product, title='Red', price=Decimal('120.00'), quantity=20)
        self.blue = Variants.objects.create(product=self.product, title='Blue', price=Decimal('130.00'), quantity=20)
        self.customer = make_user(2)
        self.now = timezone.now()

    def sale(self, **kw):
        defaults = dict(product=self.product, sale_price=Decimal('80.00'), original_price=Decimal('100.00'),
                        start_time=self.now - timedelta(hours=1), end_time=self.now + timedelta(hours=1))
        defaults.update(kw)
        return FlashSale.objects.create(**defaults)


class LivePricingTests(FlashSaleTestCase):

    def test_variant_sale_does_not_discount_other_variants(self):
        self.sale(variant=self.red, sale_price=Decimal('90.00'), original_price=Decimal('120.00'))
        self.assertEqual(line_pricing(self.product, self.red, 1)['unit_price'], Decimal('90.00'))
        self.assertEqual(line_pricing(self.product, self.blue, 1)['unit_price'], Decimal('130.00'))

    def test_variant_sale_beats_product_wide_sale(self):
        self.sale(sale_price=Decimal('95.00'))
        self.sale(variant=self.red, sale_price=Decimal('90.00'), original_price=Decimal('120.00'))
        self.assertEqual(FlashSale.live_for(self.product, self.red).sale_price, Decimal('90.00'))
        self.assertEqual(FlashSale.live_for(self.product, self.blue).sale_price, Decimal('95.00'))

    def test_cap_splits_line_between_flash_and_normal_price(self):
        self.sale(max_quantity=5, sold_count=3)
        pricing = line_pricing(self.product, None, 4)
        self.assertEqual(pricing['flash_units'], 2)
        self.assertEqual(pricing['amount'], Decimal('80.00') * 2 + Decimal('100.00') * 2)

    def test_sold_out_and_ended_sales_do_not_apply(self):
        self.sale(max_quantity=2, sold_count=2)
        self.sale(sale_price=Decimal('70.00'), end_time=self.now - timedelta(minutes=1))
        self.assertIsNone(FlashSale.live_for(self.product))
        self.assertEqual(line_pricing(self.product, None, 1)['unit_price'], Decimal('100.00'))

    def test_cart_price_ends_with_the_sale(self):
        sale = self.sale()
        cart = Cart.objects.create(user=self.customer)
        item = CartItem.objects.create(cart=cart, product=self.product, quantity=2)
        self.assertEqual(item.amount, Decimal('160.00'))

        FlashSale.objects.filter(pk=sale.pk).update(end_time=self.now - timedelta(seconds=1))
        item = CartItem.objects.get(pk=item.pk)
        self.assertEqual(item.amount, Decimal('200.00'))

    def test_legacy_locked_price_is_ignored(self):
        cart = Cart.objects.create(user=self.customer)
        item = CartItem.objects.create(cart=cart, product=self.product, quantity=1, flash_sale_price=Decimal('1.00'))
        self.assertEqual(item.price, Decimal('100.00'))


class OrderTests(FlashSaleTestCase):

    def setUp(self):
        super().setUp()
        Address.objects.create(user=self.customer, full_name='Ama', region='Greater Accra', town='Accra',
                               address='1 Test St', status=True)
        self.client = APIClient()
        self.client.force_authenticate(self.customer)

    def test_cod_order_records_flash_price_and_counts_units(self):
        sale = self.sale(max_quantity=3)
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(cart=cart, product=self.product, quantity=4)

        res = self.client.post('/api/v1/payments/place-order-cod/')
        self.assertEqual(res.status_code, 201, res.content)

        line = OrderProduct.objects.get(order_id=res.data['order_id'])
        self.assertEqual(line.amount, Decimal('80.00') * 3 + Decimal('100.00'))
        self.assertEqual(line.price, Decimal('85.00'))  # average of the split line
        sale.refresh_from_db()
        self.assertEqual(sale.sold_count, 3)
        self.assertIsNone(FlashSale.live_for(self.product))  # sold out now

    def test_checkout_works_for_product_without_brand(self):
        # A deleted brand is SET_NULL on its products; that used to fail checkout
        Product.objects.filter(pk=self.product.pk).update(brand=None)
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(cart=cart, product=self.product, quantity=2)
        res = self.client.post('/api/v1/payments/place-order-cod/')
        self.assertEqual(res.status_code, 201, res.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.total_quantity, 48)

    def test_checkout_refuses_more_than_stock(self):
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(cart=cart, product=self.product, variant=self.red, quantity=21)
        res = self.client.post('/api/v1/payments/place-order-cod/')
        self.assertEqual(res.status_code, 500)
        self.red.refresh_from_db()
        self.assertEqual(self.red.quantity, 20)
        self.assertFalse(OrderProduct.objects.exists())

    def test_paid_order_task_uses_prices_fixed_at_payment(self):
        from payments.tasks import create_order_from_payment_task
        sale = self.sale()
        address = Address.objects.get(user=self.customer)
        create_order_from_payment_task.apply(kwargs=dict(
            user_id=self.customer.id, payment_data={'amount': 16000}, payment_id=None,
            cart_items_data=[{
                'product_id': self.product.id, 'variant_id': None, 'quantity': 2,
                'delivery_option_id': None, 'amount': '160.00', 'flash_sale_id': sale.id, 'flash_units': 2,
            }],
            address_id=address.id, ip='127.0.0.1', reference='ref-1',
        ))
        line = OrderProduct.objects.get(order__user=self.customer)
        self.assertEqual((line.price, line.amount), (Decimal('80.00'), Decimal('160.00')))
        sale.refresh_from_db()
        self.assertEqual(sale.sold_count, 2)


class AdminFormTests(FlashSaleTestCase):

    def form(self, **kw):
        fmt = lambda d: timezone.localtime(d).strftime('%Y-%m-%d %H:%M:%S')
        data = dict(product=self.product.id, variant=self.red.id, sale_price='', original_price='',
                    start_time=fmt(self.now), end_time=fmt(self.now + timedelta(days=1)),
                    label='lightning', is_active='on', sold_count=0)
        data.update(kw)
        return FlashSaleAdminForm(data=data)

    def test_original_price_defaults_to_variant_price(self):
        form = self.form(sale_price='100')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.original_price, Decimal('120.00'))
        self.assertEqual(form.instance.created_by, self.vendor)

    def test_rejects_bad_input(self):
        other = Product.objects.create(title='Other', sub_category=self.sub, vendor=self.vendor,
                                       status='published', price=Decimal('10.00'))
        self.assertIn('sale_price', self.form(sale_price='150').errors)
        self.assertIn('end_time', self.form(sale_price='100', end_time=self.form().data['start_time']).errors)
        self.assertIn('variant', self.form(product=other.id, sale_price='5').errors)


class SellerFlashSaleTests(FlashSaleTestCase):

    def setUp(self):
        super().setUp()
        self.plan = SubscriptionPlan.objects.create(name='Pro Flash', tier='pro', price=Decimal('99.00'),
                                                    can_offer_discounts=True)
        VendorSubscription.objects.create(vendor=self.vendor, plan=self.plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))
        self.api = APIClient()
        self.api.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        self.api.credentials(HTTP_X_USER_TYPE='vendor')

    def payload(self, **kw):
        data = dict(product=self.product.id, sale_price='90.00',
                    start_time=(self.now + timedelta(hours=1)).isoformat(),
                    end_time=(self.now + timedelta(days=2)).isoformat())
        data.update(kw)
        return data

    def test_create_list_and_delete_scheduled_sale(self):
        res = self.api.post('/api/v1/vendor/flash-sales/', self.payload(), format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['status'], 'scheduled')
        self.assertEqual(Decimal(res.data['original_price']), Decimal('100.00'))
        sale = FlashSale.objects.get(pk=res.data['id'])
        self.assertEqual(sale.created_by, self.vendor)

        listing = self.api.get('/api/v1/vendor/flash-sales/')
        self.assertEqual([s['id'] for s in listing.data['results']], [sale.id])

        self.assertEqual(self.api.delete(f'/api/v1/vendor/flash-sales/{sale.id}/').status_code, 204)

    def test_guardrails(self):
        post = lambda **kw: self.api.post('/api/v1/vendor/flash-sales/', self.payload(**kw), format='json')
        self.assertIn('sale_price', post(sale_price='99.00').data)                      # < 5% off
        self.assertIn('original_price', post(original_price='150.00').data)             # inflated "was"
        self.assertIn('end_time', post(end_time=(self.now + timedelta(days=20)).isoformat()).data)
        self.assertIn('start_time', post(start_time=(self.now - timedelta(days=1)).isoformat()).data)

        other_vendor = Vendor.objects.create(name='Other', user=make_user(3), email='o@example.com',
                                             contact='+233550004444', is_approved=True, status='VERIFIED')
        foreign = Product.objects.create(title='Not mine', sub_category=self.sub, vendor=other_vendor,
                                         status='published', price=Decimal('50.00'))
        self.assertIn('product', post(product=foreign.id).data)

        self.assertEqual(post().status_code, 201)
        self.assertEqual(post().status_code, 400)  # overlaps the first one

    def test_started_sale_can_only_be_paused(self):
        sale = self.sale(created_by=self.vendor)
        url = f'/api/v1/vendor/flash-sales/{sale.id}/'
        self.assertEqual(self.api.patch(url, {'sale_price': '50.00'}, format='json').status_code, 400)
        res = self.api.patch(url, {'is_active': False}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['status'], 'paused')
        self.assertEqual(self.api.delete(url).status_code, 400)

    def test_plan_without_discounts_is_refused(self):
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_offer_discounts=False)
        self.assertEqual(self.api.get('/api/v1/vendor/flash-sales/').status_code, 403)

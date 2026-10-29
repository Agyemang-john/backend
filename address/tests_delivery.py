"""
address/tests_delivery.py
The `ship_country` cookie (address/delivery.py) that lets the storefront decide
"ships to you" while rendering, the product endpoints no longer doing location
lookups, cache invalidation when a product's regions change, and checkout
applying the same ISO-code rule.

Run with:  DB_HOST=localhost python manage.py test address.tests_delivery --keepdb
"""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from address import delivery
from address.models import Address, Country
from ecommerce.celery import app as celery_app
from order.models import Cart, CartItem
from product.models import Brand, Category, Main_Category, Product, Sub_Category
from userauths.models import User
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}
PASSWORD = 'Str0ng!Passw0rd'


def make_user(n):
    user = User.objects.create_user(f'Ship{n}', 'Test', f'ship{n}@example.com', f'+23324200{n:04d}', PASSWORD)
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class DeliveryCountryTestCase(TestCase):

    def setUp(self):
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

        self.ghana = Country.objects.get_or_create(code='GH', defaults={'name': 'Ghana'})[0]
        self.nigeria = Country.objects.get_or_create(code='NG', defaults={'name': 'Nigeria'})[0]
        self.niger = Country.objects.get_or_create(code='NE', defaults={'name': 'Niger'})[0]

        owner = make_user(1)
        vendor = Vendor.objects.create(
            name='Ship Shop', user=owner, email='ship-shop@example.com', contact='+233550004444',
            is_approved=True, status='VERIFIED',
        )
        main = Main_Category.objects.create(title='Ship Main')
        cat = Category.objects.create(title='Ship Cat', main_category=main)
        sub = Sub_Category.objects.create(title='Ship Sub', category=cat)
        brand = Brand.objects.create(title='Ship Brand')
        self.product = Product.objects.create(
            title='Ship Kettle', sub_category=sub, vendor=vendor, status='published',
            price=Decimal('100.00'), total_quantity=50, brand=brand,
        )
        self.customer = make_user(2)
        self.client = APIClient()

    def login(self):
        self.client.force_authenticate(self.customer)

    def address(self, country, status=True):
        return Address.objects.create(user=self.customer, full_name='Ama', country=country, status=status)

    # ── Resolving codes ──────────────────────────────────────────────────────

    def test_country_code_accepts_names_and_codes(self):
        self.assertEqual(delivery.country_code('Ghana'), 'GH')
        self.assertEqual(delivery.country_code(' ghana '), 'GH')
        self.assertEqual(delivery.country_code('ng'), 'NG')
        self.assertIsNone(delivery.country_code('Atlantis'))
        self.assertIsNone(delivery.country_code(''))

    # ── Cookie follows the default address ──────────────────────────────────

    def test_make_default_sets_cookie(self):
        self.login()
        addr = self.address('Nigeria', status=False)
        res = self.client.put('/api/v1/address/addresses/default/', {'id': addr.id}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.cookies[delivery.COOKIE].value, 'NG')
        self.assertFalse(res.cookies[delivery.COOKIE]['httponly'])

    def test_create_default_address_sets_cookie(self):
        self.login()
        res = self.client.post('/api/v1/address/addresses/', {
            'full_name': 'Ama', 'country': 'Ghana', 'region': 'Greater Accra', 'town': 'Accra',
            'address': '1 Ring Rd', 'mobile': '+233241234567', 'email': 'ama@example.com', 'status': True,
            'latitude': 5.6, 'longitude': -0.2,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.cookies[delivery.COOKIE].value, 'GH')

    def test_deleting_default_address_clears_cookie(self):
        self.login()
        addr = self.address('Ghana')
        self.client.cookies[delivery.COOKIE] = 'GH'
        res = self.client.delete(f'/api/v1/address/addresses/{addr.id}/')
        self.assertEqual(res.status_code, 204)
        self.assertEqual(res.cookies[delivery.COOKIE].value, '')
        self.assertEqual(res.cookies[delivery.COOKIE]['max-age'], 0)

    def test_unchanged_cookie_is_not_rewritten(self):
        self.login()
        addr = self.address('Ghana', status=False)
        self.client.cookies[delivery.COOKIE] = 'GH'
        res = self.client.put('/api/v1/address/addresses/default/', {'id': addr.id}, format='json')
        self.assertNotIn(delivery.COOKIE, res.cookies)

    def test_logout_clears_cookie(self):
        res = self.client.post('/api/logout/')
        self.assertIn(res.status_code, (200, 204))
        self.assertEqual(res.cookies[delivery.COOKIE]['max-age'], 0)

    # ── Product endpoints ────────────────────────────────────────────────────

    def auth_state_url(self):
        return f'/api/v1/product/{self.product.sku}/{self.product.slug}/auth-state/'

    def test_auth_state_reports_and_syncs_ship_country(self):
        self.login()
        self.address('NG')
        res = self.client.get(self.auth_state_url())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['ship_country'], 'NG')
        self.assertEqual(res.cookies[delivery.COOKIE].value, 'NG')
        self.assertNotIn('can_ship', res.data)

    def test_auth_state_without_address_clears_stale_cookie(self):
        self.login()
        self.client.cookies[delivery.COOKIE] = 'GH'
        res = self.client.get(self.auth_state_url())
        self.assertIsNone(res.data['ship_country'])
        self.assertEqual(res.cookies[delivery.COOKIE]['max-age'], 0)

    def test_guest_auth_state_does_no_location_work(self):
        with mock.patch('requests.get') as outbound:
            res = self.client.get(self.auth_state_url())
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('ship_country', res.data)
        self.assertNotIn('can_ship', res.data)
        outbound.assert_not_called()

    def test_detail_cache_refreshes_when_regions_change(self):
        url = f'/api/v1/product/{self.product.sku}/{self.product.slug}/'
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data['product']['available_in_regions'], [])
        self.assertNotIn('can_ship', first.data)

        self.product.available_in_regions.set([self.ghana])
        codes = [c['code'] for c in self.client.get(url).data['product']['available_in_regions']]
        self.assertEqual(codes, ['GH'])

        self.ghana.products.clear()  # from the Country side
        self.assertEqual(self.client.get(url).data['product']['available_in_regions'], [])

    # ── Checkout uses the same rule ──────────────────────────────────────────

    def cart_with(self, product):
        cart = Cart.objects.create(user=self.customer)
        CartItem.objects.create(cart=cart, product=product, quantity=1)
        return cart

    def test_checkout_keeps_item_for_address_stored_as_code(self):
        self.product.available_in_regions.set([self.ghana])
        cart = self.cart_with(self.product)
        self.assertEqual(cart.prevent_checkout_unavailable_products(self.address('GH')), [])
        self.assertEqual(cart.cart_items.count(), 1)

    def test_checkout_niger_does_not_match_nigeria(self):
        self.product.available_in_regions.set([self.nigeria])
        cart = self.cart_with(self.product)
        removed = cart.prevent_checkout_unavailable_products(self.address('Niger'))
        self.assertEqual(len(removed), 1)
        self.assertEqual(cart.cart_items.count(), 0)

    def test_checkout_unrestricted_product_ships_anywhere(self):
        cart = self.cart_with(self.product)
        self.assertEqual(cart.prevent_checkout_unavailable_products(self.address('Nigeria')), [])

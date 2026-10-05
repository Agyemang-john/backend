"""
vendor/tests.py

Run with:  DB_HOST=localhost python manage.py test vendor.tests --keepdb
"""

from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from product.models import Category, Main_Category, Product, Sub_Category
from userauths.models import User
from vendor.models import Vendor

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

USD_RATE = Decimal('0.1')


@override_settings(**TEST_SETTINGS)
class SellerStoreProductsTests(TestCase):
    """The public seller storefront grid: paging, filters, sorting, currency."""

    @classmethod
    def setUpTestData(cls):
        user = User.objects.create(
            first_name='Shop', last_name='Owner', email='owner@example.com',
            phone='+233550009999', role='vendor', is_active=True,
        )
        cls.vendor = Vendor.objects.create(
            name='Store Test Shop', user=user, email='store@example.com', contact='+233550001111',
            is_approved=True, is_subscribed=True, shop_paused=False, is_suspended=False,
        )
        main = Main_Category.objects.create(title='General')
        category = Category.objects.create(title='Fashion', main_category=main)
        cls.shirts = Sub_Category.objects.create(title='Shirts', category=category)
        cls.shoes = Sub_Category.objects.create(title='Shoes', category=category)

        # 20 shirts priced 10..29 GHS (every other one on sale), 5 shoes at 100+.
        for n in range(20):
            Product.objects.create(
                title=f'Store Shirt {n}', sub_category=cls.shirts, vendor=cls.vendor,
                status='published', price=Decimal(10 + n),
                old_price=Decimal(50) if n % 2 == 0 else Decimal(5),
                avg_rating=4.5 if n < 3 else 3.0, review_count=n,
            )
        for n in range(5):
            Product.objects.create(
                title=f'Store Shoe {n}', sub_category=cls.shoes, vendor=cls.vendor,
                status='published', price=Decimal(100 + n), old_price=Decimal(1),
            )
        Product.objects.create(
            title='Store Draft', sub_category=cls.shirts, vendor=cls.vendor,
            status='in_review', price=Decimal(1),
        )

    def setUp(self):
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1.0, 'USD': float(USD_RATE)}, 3600)
        self.client = APIClient()
        self.url = reverse('vendor-products', kwargs={'slug': self.vendor.slug})

    def get(self, currency='GHS', **params):
        response = self.client.get(self.url, params, HTTP_X_CURRENCY=currency)
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def test_sixteen_per_page(self):
        first = self.get()
        self.assertEqual(first['total'], 25)  # the draft is excluded
        self.assertEqual(first['total_pages'], 2)
        self.assertEqual(len(first['results']), 16)
        self.assertEqual(len(self.get(page=2)['results']), 9)

    def test_page_out_of_range_clamps_to_last_page(self):
        self.assertEqual(self.get(page=99)['page'], 2)

    def test_query_count_does_not_grow_with_products(self):
        with CaptureQueriesContext(connection) as queries:
            self.get()
        # vendor id + count + page + category facets
        self.assertLessEqual(len(queries), 4)

    def test_card_fields(self):
        card = self.get()['results'][0]
        self.assertEqual(
            set(card),
            {'id', 'title', 'slug', 'sku', 'image', 'price', 'old_price',
             'average_rating', 'review_count', 'currency'},
        )

    def test_prices_are_converted_to_the_requested_currency(self):
        ghs = self.get(sort='price_asc')['results'][0]
        usd = self.get(currency='USD', sort='price_asc')['results'][0]
        self.assertEqual(ghs['currency'], 'GHS')
        self.assertEqual(usd['currency'], 'USD')
        self.assertEqual(Decimal(str(ghs['price'])), Decimal('10.00'))
        self.assertEqual(Decimal(str(usd['price'])), Decimal('1.00'))

    def test_price_filter_is_in_the_shoppers_currency(self):
        # $1.50–$2.00 == GHS 15–20 → shirts 15..20
        data = self.get(currency='USD', min_price='1.5', max_price='2')
        self.assertEqual(data['total'], 6)
        for card in data['results']:
            self.assertTrue(Decimal('1.5') <= Decimal(str(card['price'])) <= Decimal('2'))

    def test_category_filter_and_facets(self):
        data = self.get(category=self.shoes.slug)
        self.assertEqual(data['total'], 5)
        self.assertEqual(
            [(c['slug'], c['count']) for c in data['categories']],
            [(self.shirts.slug, 20), (self.shoes.slug, 5)],
        )

    def test_on_sale_and_rating_filters(self):
        self.assertEqual(self.get(on_sale=1)['total'], 10)
        self.assertEqual(self.get(rating=4)['total'], 3)

    def test_sorting(self):
        prices = [Decimal(str(c['price'])) for c in self.get(sort='price_desc')['results']]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_garbage_params_are_ignored(self):
        data = self.get(page='x', sort='drop table', min_price='-5', rating='abc')
        self.assertEqual(data['total'], 25)

    def test_cached_page_still_converts_per_request(self):
        self.get(sort='price_asc')  # warm the cache in GHS
        usd = self.get(currency='USD', sort='price_asc')['results'][0]
        self.assertEqual(Decimal(str(usd['price'])), Decimal('1.00'))

    def test_unknown_seller_is_404(self):
        response = self.client.get(reverse('vendor-products', kwargs={'slug': 'no-such-shop'}))
        self.assertEqual(response.status_code, 404)

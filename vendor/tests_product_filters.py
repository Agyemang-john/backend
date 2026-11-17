"""
vendor/tests_product_filters.py
Seller dashboard product grid: search, filters, sort, facets and the 50-per-page
default (vendor/product_filters.py via ProductListCreateView).

Run with:  DB_HOST=localhost python manage.py test vendor.tests_product_filters --keepdb
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from product.models import Brand, Category, Main_Category, Product, Sub_Category
from vendor.models import Vendor
from vendor.tests_operations import TEST_SETTINGS, make_user, vendor_client

URL = '/api/v1/vendor/products/'


@override_settings(**TEST_SETTINGS)
class ProductFilterTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1})
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(
            name='Filter Shop', user=self.owner, email='filter@example.com', contact='+233550004444',
            is_approved=True, status='VERIFIED', low_stock_threshold=5,
        )
        main = Main_Category.objects.create(title='F Main')
        cat = Category.objects.create(title='F Cat', main_category=main)
        self.dresses = Sub_Category.objects.create(title='Dresses', category=cat)
        self.shoes = Sub_Category.objects.create(title='Shoes', category=cat)
        self.brand = Brand.objects.create(title='Kente Co', slug='kente-co')

        def make(title, sub, price, qty, **kw):
            return Product.objects.create(
                title=title, sub_category=sub, vendor=self.vendor, status=kw.pop('status', 'published'),
                price=Decimal(price), total_quantity=qty, **kw,
            )

        self.red = make('Red Midi Dress', self.dresses, '120.00', 30, seller_sku='RD-001', brand=self.brand)
        self.blue = make('Blue Maxi Dress', self.dresses, '200.00', 3, seller_sku='BD-002')
        self.boot = make('Leather Boot', self.shoes, '350.00', 0, seller_sku='LB-900', status='draft',
                         fulfilment_type='imported', gtin='0123456789012')
        self.client = vendor_client(self.owner)

    def ids(self, query=''):
        response = self.client.get(f'{URL}?{query}')
        self.assertEqual(response.status_code, 200, response.content)
        return [row['id'] for row in response.json()['results']]

    def test_default_page_size_is_50(self):
        for i in range(52):
            Product.objects.create(title=f'Filler {i}', vendor=self.vendor, price=Decimal('1'), total_quantity=1)
        data = self.client.get(URL).json()
        self.assertEqual(len(data['results']), 50)
        self.assertEqual(data['page_size'], 50)
        self.assertEqual(data['total_pages'], 2)

    def test_search_matches_every_word_in_any_order(self):
        self.assertEqual(self.ids('search=dress red'), [self.red.id])

    def test_search_partial_sku_brand_and_gtin(self):
        self.assertEqual(self.ids('search=lb-9'), [self.boot.id])
        self.assertEqual(self.ids('search=kente'), [self.red.id])
        self.assertEqual(self.ids('search=0123456789012'), [self.boot.id])

    def test_stock_category_fulfilment_and_price_filters(self):
        self.assertEqual(self.ids('stock=low'), [self.blue.id])
        self.assertEqual(self.ids('stock=out'), [self.boot.id])
        self.assertCountEqual(self.ids('stock=in'), [self.red.id, self.blue.id])
        self.assertEqual(self.ids(f'category={self.shoes.id}'), [self.boot.id])
        self.assertEqual(self.ids('fulfilment=imported'), [self.boot.id])
        self.assertEqual(self.ids('min_price=150&max_price=300'), [self.blue.id])
        # Junk values are ignored rather than 500-ing.
        self.assertEqual(len(self.ids('min_price=abc&category=x&fulfilment=nope&sort=bogus')), 3)

    def test_filters_combine(self):
        self.assertEqual(self.ids(f'category={self.dresses.id}&stock=low&search=dress'), [self.blue.id])

    def test_sorts(self):
        self.assertEqual(self.ids('sort=price_low'), [self.red.id, self.blue.id, self.boot.id])
        self.assertEqual(self.ids('sort=price_high'), [self.boot.id, self.blue.id, self.red.id])
        self.assertEqual(self.ids('sort=stock_low'), [self.boot.id, self.blue.id, self.red.id])
        self.assertEqual(self.ids('sort=title'), [self.blue.id, self.boot.id, self.red.id])

    def test_facets_ignore_current_filters(self):
        data = self.client.get(f'{URL}?search=boot').json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(
            data['categories'],
            [{'id': self.dresses.id, 'title': 'Dresses', 'count': 2},
             {'id': self.shoes.id, 'title': 'Shoes', 'count': 1}],
        )
        self.assertEqual(data['stock_counts'], {'in': 2, 'low': 1, 'out': 1})
        self.assertEqual(data['low_stock_threshold'], 5)

    def test_other_vendors_products_never_leak(self):
        other_user = make_user(2)
        other = Vendor.objects.create(
            name='Other Shop', user=other_user, email='other@example.com', contact='+233550005555',
            is_approved=True, status='VERIFIED',
        )
        Product.objects.create(title='Red Midi Dress Copy', vendor=other, price=Decimal('1'), total_quantity=1)
        self.assertEqual(self.ids('search=red'), [self.red.id])

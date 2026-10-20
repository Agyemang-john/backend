from decimal import Decimal

from django.core.cache import cache
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from product.listing import DEFAULT_PAGE_SIZE, listing_page_size
from product.models import Category, Main_Category, Product, Sub_Category
from userauths.models import User
from vendor.models import Vendor


class ListingPageSizeTests(SimpleTestCase):
    def size(self, query):
        return listing_page_size(QueryDict(query))

    def test_defaults_to_thirty(self):
        self.assertEqual(DEFAULT_PAGE_SIZE, 30)
        self.assertEqual(self.size(''), 30)

    def test_accepts_the_offered_sizes(self):
        for size in (30, 60, 90):
            self.assertEqual(self.size(f'page_size={size}'), size)

    def test_anything_else_falls_back_to_the_default(self):
        for raw in ('12', '10000', '0', '-30', 'abc', ''):
            self.assertEqual(self.size(f'page_size={raw}'), 30)


TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
}


@override_settings(**TEST_SETTINGS)
class CategoryPageSizeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = User.objects.create(
            first_name='Page', last_name='Size', email='pagesize@example.com',
            phone='+233550008888', role='vendor', is_active=True,
        )
        vendor = Vendor.objects.create(
            user=user, name='Page Size Store', email='pagesize@example.com',
            is_approved=True, is_subscribed=True, shop_paused=False, is_suspended=False,
        )
        main = Main_Category.objects.create(title='Paging')
        category = Category.objects.create(title='Paging Things', main_category=main)
        cls.sub = Sub_Category.objects.create(title='Paged Widgets', category=category)
        for n in range(35):
            Product.objects.create(
                title=f'Widget {n}', sub_category=cls.sub, vendor=vendor,
                status='published', price=Decimal(10 + n),
            )

    def setUp(self):
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1.0}, 3600)
        self.url = reverse('category', kwargs={'slug': self.sub.slug})

    def get(self, **params):
        response = APIClient().get(self.url, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def test_thirty_per_page_by_default(self):
        data = self.get()
        self.assertEqual((data['total'], data['page_size'], data['total_pages']), (35, 30, 2))
        self.assertEqual(len(data['products_with_details']), 30)
        self.assertEqual(len(self.get(page=2)['products_with_details']), 5)

    def test_chosen_page_size_is_used_and_kept_in_next_link(self):
        data = self.get(page_size=60)
        self.assertEqual((data['page_size'], data['total_pages']), (60, 1))
        self.assertEqual(len(data['products_with_details']), 35)
        self.assertIn('page_size=30', self.get(page_size=30)['next'])

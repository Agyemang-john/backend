"""
recommendation/tests_scale.py

Tests for the growth work: the Today's Deals page, category shelves, the live
"Recommended for you" blend, learning from rail clicks and skips, and the
department-restricted neighbour search.

Run with the dotted path: `manage.py test recommendation.tests_scale`.
"""

from datetime import timedelta
from decimal import Decimal

import numpy as np
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from order.models import Order, OrderProduct
from product.models import (
    Category, Collection, FlashSale, Main_Category, Product, RecentlyViewedProduct,
)

from . import engagement, serving, similarity
from .models import (
    EVENT_CLICK, EVENT_IMPRESSION, SURFACE_FOR_YOU, ProductNeighbor, RecommendationEvent,
    UserRecommendation,
)
from .tests import CatalogMixin, RecommenderTestCase, make_user
from .train import run_deal_scoring, run_training


class DealsPageTests(CatalogMixin, RecommenderTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=6)
        # Move the Laptops category into its own department.
        cls.electronics = Main_Category.objects.create(title='Electronics')
        cls.laptops = Category.objects.get(title='Laptops Category')
        cls.laptops.main_category = cls.electronics
        cls.laptops.save()
        cls.general = Main_Category.objects.get(title='General')

        # One deep discount: 60% off.
        cls.deep = cls.products[0]
        Product.objects.filter(pk=cls.deep.pk).update(price=Decimal('40.00'), old_price=Decimal('100.00'))

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        run_deal_scoring()

    def get(self, **params):
        response = self.client.get(reverse('rec-deals'), params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_lists_every_eligible_deal_with_department_counts(self):
        data = self.get()
        self.assertEqual(data['total'], len(self.products))
        counts = {d['title']: d['count'] for d in data['departments']}
        self.assertEqual(counts, {'General': 12, 'Electronics': 6})
        self.assertEqual(len(data['results']), min(24, len(self.products)))

    def test_department_filter_and_its_categories(self):
        data = self.get(department=self.electronics.id)
        self.assertEqual(data['total'], 6)
        laptop_ids = {p.id for p in self.products if p.sub_category.category_id == self.laptops.id}
        self.assertTrue({card['id'] for card in data['results']} <= laptop_ids)
        self.assertEqual([c['title'] for c in data['categories']], ['Laptops Category'])
        # The department chips still show every department, not only the chosen one.
        self.assertEqual(len(data['departments']), 2)

    def test_discount_filter_and_bucket_counts(self):
        data = self.get(discount=50)
        self.assertEqual([card['id'] for card in data['results']], [self.deep.id])
        buckets = {b['value']: b['count'] for b in self.get()['discount_buckets']}
        self.assertEqual(buckets[50], 1)
        self.assertEqual(buckets[25], len(self.products))

    def test_price_sort(self):
        prices = [float(card['deal_price']) for card in self.get(sort='price_asc')['results']]
        self.assertEqual(prices, sorted(prices))

    def test_flash_filter_and_end_time(self):
        product = self.products[5]
        FlashSale.objects.create(
            product=product, sale_price=Decimal('50.00'), original_price=Decimal('150.00'),
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=3),
        )
        run_deal_scoring()
        data = self.get(flash=1)
        self.assertEqual([card['id'] for card in data['results']], [product.id])
        self.assertIsNotNone(data['results'][0]['ends_at'])
        self.assertEqual(data['flash_count'], 1)

    def test_deals_collection_supplies_the_banner(self):
        Collection.objects.create(slug='todays-deals', title='Mega Deals Week', filter_type='todays_deals')
        self.assertEqual(self.get()['collection']['title'], 'Mega Deals Week')

    def test_prices_follow_the_currency_header(self):
        ghs = self.client.get(reverse('rec-deals')).json()
        usd = self.client.get(reverse('rec-deals'), HTTP_X_CURRENCY='USD').json()
        self.assertAlmostEqual(usd['min_price_unfiltered'], round(ghs['min_price_unfiltered'] * 0.094, 2), places=2)

    def test_pagination_has_no_duplicates(self):
        seen = []
        data = self.get()
        for page in range(1, data['pages'] + 1):
            seen += [card['id'] for card in self.get(page=page)['results']]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), data['total'])


class DiversifyTests(RecommenderTestCase):

    def test_caps_sellers_then_fills_with_overflow(self):
        facts = {pid: (1 if pid < 10 else 2, pid, None) for pid in range(20)}
        ids = list(range(20))
        picked = serving.diversify(ids, facts, 6, max_per_vendor=2, max_per_sub_category=9)
        self.assertEqual(picked[:4], [0, 1, 10, 11])
        self.assertEqual(len(picked), 6)


class CategoryShelfTests(CatalogMixin, RecommenderTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=6)
        cls.shopper = make_user(1)
        cls.buyer = make_user(2)
        shoes = [p for p in cls.products if 'shoe' in p.title][:5]
        for product in shoes:
            order = Order.objects.create(user=cls.buyer, total=product.price, is_ordered=True, status='delivered')
            OrderProduct.objects.create(order=order, product=product, quantity=1, price=product.price, amount=product.price)
        cls.shoes = shoes

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_shopper_gets_best_sellers_from_the_category_they_browse(self):
        RecentlyViewedProduct.objects.create(user=self.shopper, product=self.shoes[0])
        self.client.force_authenticate(self.shopper)
        shelves = self.client.get(reverse('rec-category-shelves')).json()['shelves']
        self.assertEqual(shelves[0]['title'], 'Best sellers in Shoes Category')
        self.assertEqual(shelves[0]['kind'], 'best_sellers')
        self.assertTrue({p.id for p in self.shoes} >= {card['id'] for card in shelves[0]['results']})

    def test_guest_gets_popular_categories_without_repeats(self):
        shelves = self.client.get(reverse('rec-category-shelves')).json()['shelves']
        self.assertGreaterEqual(len(shelves), 2)
        ids = [card['id'] for shelf in shelves for card in shelf['results']]
        self.assertEqual(len(ids), len(set(ids)))


class LiveForYouTests(CatalogMixin, RecommenderTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=8)
        cls.shopper = make_user(1)

    def test_recent_views_reshape_the_nightly_list(self):
        run_training(evaluate=False)
        shoes = [p for p in self.products if 'shoe' in p.title]
        blenders = [p for p in self.products if 'blender' in p.title]

        # The nightly list says shoes.
        UserRecommendation.objects.bulk_create([
            UserRecommendation(user=self.shopper, product=p, surface=SURFACE_FOR_YOU, rank=rank,
                               generated_at=timezone.now() - timedelta(hours=6))
            for rank, p in enumerate(shoes)
        ])
        # This morning they looked at a blender.
        RecentlyViewedProduct.objects.create(user=self.shopper, product=blenders[0])
        self.assertTrue(ProductNeighbor.objects.filter(product=blenders[0]).exists())

        request = APIClient().get(reverse('rec-keep-shopping')).wsgi_request
        request.user = self.shopper
        products, reasons = serving.recommended_for_you(request, limit=6)

        top = products[0]
        self.assertIn('blender', top.title)
        self.assertNotIn(blenders[0].id, [p.id for p in products], 'just-viewed product repeated')
        self.assertTrue(reasons[top.id].startswith('Because you viewed'))
        self.assertTrue(any('shoe' in p.title for p in products), 'nightly list was dropped')


class EngagementTests(CatalogMixin, RecommenderTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=2)
        cls.shopper = make_user(1)

    def _events(self, product, event_type, position, n, user=None):
        RecommendationEvent.objects.bulk_create([
            RecommendationEvent(product=product, surface=SURFACE_FOR_YOU, event_type=event_type,
                                position=position, user=user, date=timezone.now().date())
            for _ in range(n)
        ])

    def test_lift_corrects_for_position(self):
        top_slot, low_slot = self.products[0], self.products[1]
        # Slot 0 is clicked 20% of the time on average; slot 10 only 2%.
        self._events(top_slot, EVENT_IMPRESSION, 0, 200)
        self._events(top_slot, EVENT_CLICK, 0, 30)
        self._events(self.products[2], EVENT_IMPRESSION, 0, 200)
        self._events(self.products[2], EVENT_CLICK, 0, 50)
        self._events(low_slot, EVENT_IMPRESSION, 10, 200)
        self._events(low_slot, EVENT_CLICK, 10, 15)
        self._events(self.products[3], EVENT_IMPRESSION, 10, 200)

        lift = engagement.product_lift()
        # Fewer raw clicks than the top-slot product, but far more than its slot predicts.
        self.assertGreater(lift[low_slot.id], 1.0)
        self.assertLess(lift[top_slot.id], 1.0)
        self.assertLessEqual(max(lift.values()), engagement.LIFT_RANGE[1])

    def test_repeatedly_ignored_products_count_as_skipped(self):
        ignored, clicked = self.products[0], self.products[1]
        self._events(ignored, EVENT_IMPRESSION, 0, 3, user=self.shopper)
        self._events(clicked, EVENT_IMPRESSION, 1, 3, user=self.shopper)
        self._events(clicked, EVENT_CLICK, 1, 1, user=self.shopper)

        self.assertEqual(engagement.skipped_by_user(), {self.shopper.id: {ignored.id}})
        request = APIClient().get(reverse('rec-keep-shopping')).wsgi_request
        request.user = self.shopper
        self.assertEqual(engagement.skipped_for_request(request), {ignored.id})


class DepartmentNeighbourTests(RecommenderTestCase):

    def test_large_departments_search_only_themselves(self):
        rng = np.random.default_rng(0)
        n = similarity.MIN_GROUP_SIZE + 10
        # Two departments with *identical* content distributions: without the
        # department restriction they would mix freely.
        vectors = rng.normal(size=(2 * n + 5, 16)).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        groups = np.array([1] * n + [2] * n + [0] * 5)
        ids = list(range(len(groups)))

        hybrid, content = similarity.compute_neighbors(
            ids, np.zeros((len(ids), 0), np.float32), vectors,
            np.zeros(len(ids)), cf_trust=0.0, groups=groups,
        )
        for row in range(2 * n):
            for neighbour, _ in hybrid.get(row, []):
                self.assertEqual(groups[neighbour], groups[row])
                self.assertNotEqual(neighbour, row)
        # Uncategorised products may match anything.
        reach = {groups[nb] for row in range(2 * n, len(ids)) for nb, _ in hybrid.get(row, [])}
        self.assertTrue(reach - {0})
        self.assertEqual(hybrid, content)

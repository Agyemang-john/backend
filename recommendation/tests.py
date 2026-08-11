"""
recommendation/tests.py

End-to-end tests for the recommender.

The emphasis is deliberately on the properties that make a recommender safe to
run in production rather than on unit-level minutiae:

  * every rail returns something, in every state — empty database, untrained
    model, guest visitor, cold-start user
  * training never raises, whatever the data looks like
  * the deal guardrails actually exclude what they claim to
  * a model that has learned structure demonstrably beats showing everyone the
    best-sellers

A recommender that returns an empty list is indistinguishable from a broken page,
so "never empty" is tested as hard as "correct".
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app

# Run queued tasks inline. Creating an Order fires seller/customer notification
# tasks through .delay(), which otherwise blocks trying to reach the Redis broker
# — a host that only resolves inside docker-compose.
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = False

#: Local cache and email backends so tests never touch shared infrastructure.
TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

from order.models import Cart, CartItem, Order, OrderProduct
from product.models import (
    Brand, Category, Main_Category, Product, ProductViewLog, RecentlyViewedProduct,
    Sub_Category,
)
from userauths.models import User
from vendor.models import Vendor

from . import deals, serving
from .models import (
    ModelRun, NotInterested, ProductDealScore, ProductEmbedding, ProductNeighbor,
    ProductPriceHistory, RecommendationEvent, UserRecommendation,
)
from .train import _choose_hyperparameters, run_deal_scoring, run_training


def make_user(n: int, **kwargs) -> User:
    kwargs.setdefault('role', 'customer')
    kwargs.setdefault('is_active', True)
    user = User.objects.create(
        first_name=f"Test{n}", last_name='User',
        email=f"shopper{n}@example.com", phone=f"+2335500{n:05d}",
        **kwargs
    )
    user.set_password('x')
    user.save()
    return user


@override_settings(**TEST_SETTINGS)
class RecommenderTestCase(TestCase):
    """
    Base case: an isolated cache, and exchange rates pre-seeded.

    Seeding the rates matters — the serializers convert prices through
    core.service.get_exchange_rates(), which calls an external API on a cache
    miss. Left alone that is a live network round trip per test, and it dominated
    the suite's runtime before this was added.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1.0, 'USD': 0.094, 'EUR': 0.087}, 3600)


class CatalogMixin:
    """Builds a small but structurally realistic catalog: 3 groups × N products."""

    GROUPS = ['Shoes', 'Laptops', 'Blenders']
    KEYWORDS = {
        'Shoes':    'running shoe sneaker trainer footwear',
        'Laptops':  'laptop computer notebook ultrabook',
        'Blenders': 'kitchen blender mixer smoothie',
    }

    #: Several sellers, because a single-vendor fixture makes the per-vendor
    #: diversity cap fire on every candidate and hides what the cap actually does.
    N_VENDORS = 4

    @classmethod
    def build_catalog(cls, per_group=12):
        vendors = []
        for n in range(cls.N_VENDORS):
            vendor_user = make_user(9000 + n, role='vendor')
            vendors.append(Vendor.objects.create(
                name=f"Test Shop {n}", user=vendor_user, email=f"shop{n}@example.com",
                contact=f"+23355000{n:04d}", is_approved=True, is_subscribed=True,
                shop_paused=False, is_suspended=False,
            ))

        main = Main_Category.objects.create(title='General')
        brands = [Brand.objects.create(title=f"Brand{n}") for n in range(3)]

        products = []
        for group_index, group in enumerate(cls.GROUPS):
            category = Category.objects.create(title=f"{group} Category", main_category=main)
            sub_category = Sub_Category.objects.create(title=group, category=category)
            for n in range(per_group):
                index = group_index * per_group + n
                products.append(Product.objects.create(
                    title=f"{cls.KEYWORDS[group]} model {index}",
                    sub_category=sub_category,
                    vendor=vendors[index % cls.N_VENDORS],
                    brand=brands[index % len(brands)],
                    status='published',
                    price=Decimal('100.00') + index,
                    old_price=Decimal('150.00') + index,
                    total_quantity=50,
                    description=f"<p>A quality {group.lower()} product</p>",
                    features=f"<p>{cls.KEYWORDS[group]}</p>",
                    specifications=f"<p>{cls.KEYWORDS[group]} specification</p>",
                ))
        return vendors[0], products


class EmptyDatabaseTests(RecommenderTestCase):
    """
    The system must be deployable before there is any data at all. Every one of
    these paths runs on a fresh install, and none of them may raise.
    """

    def test_training_on_empty_database_completes(self):
        run = run_training(evaluate=True)
        self.assertEqual(run.status, ModelRun.STATUS_COMPLETED)
        self.assertEqual(run.n_interactions, 0)

    def test_deal_scoring_on_empty_database(self):
        self.assertEqual(run_deal_scoring(), 0)

    def test_rails_return_empty_lists_not_errors(self):
        client = APIClient()
        for name in ('rec-todays-deals', 'rec-for-you', 'rec-keep-shopping', 'rec-cart-addons'):
            with self.subTest(rail=name):
                response = client.get(reverse(name))
                self.assertEqual(response.status_code, 200)


class HyperparameterTests(RecommenderTestCase):
    """
    Model capacity must track the evidence available.

    Measured on synthetic data with known structure, factors=48 at ~43
    interactions/shopper drove item-neighbour purity to chance while factors=8
    scored 1.00. Capacity is the dominant knob, so these bounds are asserted
    rather than left to drift.
    """

    def _dataset(self, n_users, n_items, interactions):
        """
        A matrix with exactly `interactions` distinct filled cells.

        Drawn without replacement from the flattened index space — modular
        arithmetic on (row, col) silently collapses to gcd(n_users, n_items)
        distinct pairs, which would make the density this test is checking a
        fiction.
        """
        import numpy as np
        from scipy.sparse import csr_matrix

        from .dataset import Dataset

        rng = np.random.default_rng(0)
        interactions = min(interactions, n_users * n_items)
        flat = rng.choice(n_users * n_items, size=interactions, replace=False)
        rows, cols = np.divmod(flat, n_items)

        matrix = csr_matrix(
            (np.ones(interactions, dtype=np.float32), (rows, cols)),
            shape=(n_users, n_items),
        )
        assert matrix.nnz == interactions, 'test fixture failed to produce distinct pairs'
        return Dataset(
            matrix=matrix, user_keys=[], item_ids=[], user_pos={}, item_pos={},
        )

    def test_sparse_data_gets_small_model(self):
        params = _choose_hyperparameters(self._dataset(200, 100, 1_500))
        self.assertLessEqual(params['factors'], 8)
        self.assertGreaterEqual(params['regularization'], 0.10)

    def test_dense_data_gets_larger_model(self):
        params = _choose_hyperparameters(self._dataset(2_000, 1_000, 120_000))
        self.assertGreaterEqual(params['factors'], 16)

    def test_factors_never_exceed_matrix_dimensions(self):
        params = _choose_hyperparameters(self._dataset(5, 4, 20))
        self.assertLessEqual(params['factors'], 3)
        self.assertGreaterEqual(params['factors'], 2)


class DealScoringTests(CatalogMixin, RecommenderTestCase):

    def setUp(self):
        super().setUp()
        self.vendor, self.products = self.build_catalog(per_group=6)

    def test_out_of_stock_products_are_excluded(self):
        product = self.products[0]
        product.total_quantity = 0
        product.save()

        run_deal_scoring()
        score = ProductDealScore.objects.get(product=product)
        self.assertFalse(score.is_eligible)
        self.assertIn('stock', score.ineligible_reason)

    def test_products_without_a_real_discount_are_excluded(self):
        product = self.products[1]
        product.old_price = product.price
        product.save()

        run_deal_scoring()
        score = ProductDealScore.objects.get(product=product)
        self.assertFalse(score.is_eligible)
        self.assertIn('discount', score.ineligible_reason)

    def test_absurd_discounts_are_rejected_as_pricing_errors(self):
        product = self.products[2]
        product.price = Decimal('1.00')
        product.old_price = Decimal('1000.00')       # 99.9% off
        product.save()

        run_deal_scoring()
        score = ProductDealScore.objects.get(product=product)
        self.assertFalse(score.is_eligible)

    def test_inflated_old_price_is_penalised_against_price_history(self):
        """
        The anti-gaming check. Two products advertise the same discount; one has
        genuinely been marked down, the other has sat at this price for a month
        with an inflated reference price. The real markdown must rank higher.
        """
        honest, inflated = self.products[3], self.products[4]
        for product in (honest, inflated):
            product.price = Decimal('60.00')
            product.old_price = Decimal('100.00')     # both claim 40% off
            product.save()

        today = timezone.now().date()
        for day in range(1, 31):
            date = today - timedelta(days=day)
            # The honest product was ₵100 until recently — a real drop.
            ProductPriceHistory.objects.create(
                product=honest, date=date, price=Decimal('100.00'), old_price=Decimal('100.00'),
            )
            # The inflated one has always been ₵60; old_price is decoration.
            ProductPriceHistory.objects.create(
                product=inflated, date=date, price=Decimal('60.00'), old_price=Decimal('100.00'),
            )

        deals.compute_deal_scores()

        honest_score = ProductDealScore.objects.get(product=honest)
        inflated_score = ProductDealScore.objects.get(product=inflated)

        self.assertGreater(honest_score.price_percentile, inflated_score.price_percentile)
        self.assertGreater(honest_score.discount_component, inflated_score.discount_component)
        self.assertGreater(honest_score.score, inflated_score.score)

    def test_price_snapshot_is_idempotent_within_a_day(self):
        deals.snapshot_prices()
        first = ProductPriceHistory.objects.count()
        deals.snapshot_prices()
        self.assertEqual(ProductPriceHistory.objects.count(), first)

    def test_deals_endpoint_returns_scored_deals(self):
        run_deal_scoring()
        response = APIClient().get(reverse('rec-todays-deals'), {'limit': 5})
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data['results']), 0)
        self.assertIn('savings_percent', response.data['results'][0])


class TrainingPipelineTests(CatalogMixin, RecommenderTestCase):
    """Full pipeline against a catalog with real behavioural structure."""

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=12)
        cls.shoppers = [make_user(n) for n in range(30)]

        # Each shopper browses and buys within one group — the structure the
        # model is supposed to discover.
        now = timezone.now()
        for index, shopper in enumerate(cls.shoppers):
            group = index % 3
            group_products = cls.products[group * 12:(group + 1) * 12]

            for product in group_products[:8]:
                ProductViewLog.objects.create(
                    product=product, visitor_key=f"u:{shopper.pk}", user=shopper,
                    is_bot=False, is_returning=False, device_type='desktop',
                    date=now.date(),
                )
                RecentlyViewedProduct.objects.create(user=shopper, product=product)

            cart = Cart.objects.create(user=shopper)
            for product in group_products[:3]:
                CartItem.objects.create(cart=cart, product=product, quantity=1)

            order = Order.objects.create(
                user=shopper, total=Decimal('300.00'), is_ordered=True, status='delivered',
            )
            for product in group_products[:2]:
                OrderProduct.objects.create(
                    order=order, product=product, quantity=1,
                    price=product.price, amount=product.price,
                )

    def test_full_training_run_populates_every_table(self):
        run = run_training(evaluate=True)

        self.assertEqual(run.status, ModelRun.STATUS_COMPLETED)
        self.assertGreater(run.n_interactions, 0)
        self.assertGreater(run.n_users, 0)

        self.assertEqual(ProductEmbedding.objects.count(), len(self.products))
        self.assertGreater(ProductNeighbor.objects.count(), 0)
        self.assertGreater(UserRecommendation.objects.count(), 0)

    def test_every_product_gets_a_content_vector(self):
        run_training(evaluate=False)
        for embedding in ProductEmbedding.objects.all():
            self.assertIsNotNone(embedding.content, f"product {embedding.product_id} has no content vector")

    def test_neighbours_respect_category_structure(self):
        """
        Products in the same group must dominate each other's neighbour lists.
        This is the single most visible quality signal on the storefront — it is
        what a shopper sees under "You might also like this".
        """
        run_training(evaluate=False)

        sub_category_of = dict(Product.objects.values_list('id', 'sub_category_id'))
        matches = total = 0
        for neighbor in ProductNeighbor.objects.filter(kind='hybrid'):
            total += 1
            if sub_category_of[neighbor.product_id] == sub_category_of[neighbor.neighbor_id]:
                matches += 1

        self.assertGreater(total, 0)
        purity = matches / total
        self.assertGreater(purity, 0.6, f"neighbour purity {purity:.1%} — chance is ~33%")

    def test_recommendations_exclude_what_the_shopper_already_has(self):
        run_training(evaluate=False)
        shopper = self.shoppers[0]

        interacted = set(
            RecentlyViewedProduct.objects.filter(user=shopper).values_list('product_id', flat=True)
        )
        recommended = set(
            UserRecommendation.objects.filter(user=shopper).values_list('product_id', flat=True)
        )
        self.assertFalse(interacted & recommended, 'recommended a product the shopper already viewed')

    def test_recommendations_carry_an_explanation(self):
        run_training(evaluate=False)
        rows = UserRecommendation.objects.exclude(reason_detail='')
        self.assertGreater(rows.count(), 0, 'no recommendation carried a shopper-facing reason')

    def test_diversity_caps_are_enforced(self):
        run_training(evaluate=False)
        from .ranker import MAX_PER_SUB_CATEGORY

        sub_category_of = dict(Product.objects.values_list('id', 'sub_category_id'))
        for shopper in self.shoppers[:5]:
            top = list(
                UserRecommendation.objects.filter(user=shopper)
                .order_by('rank').values_list('product_id', flat=True)[:9]
            )
            if len(top) < 9:
                continue
            counts: dict[int, int] = {}
            for product_id in top:
                key = sub_category_of[product_id]
                counts[key] = counts.get(key, 0) + 1
            self.assertLessEqual(
                max(counts.values()), MAX_PER_SUB_CATEGORY,
                'diversity cap breached in the top of the rail',
            )

    def test_run_is_only_visible_to_serving_once_completed(self):
        running = ModelRun.objects.create(status=ModelRun.STATUS_RUNNING)
        cache.delete('rec:model_version')
        self.assertNotEqual(serving.current_model_version(), running.pk)


class ServingTests(CatalogMixin, RecommenderTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=8)
        cls.shopper = make_user(1)

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_similar_products_work_without_a_trained_model(self):
        """Before any training run, similarity falls back to the sub-category."""
        product = self.products[0]
        results = serving.you_might_also_like(product.id, limit=6)
        self.assertGreater(len(results), 0)
        self.assertNotIn(product.id, [p.id for p in results])

    def test_guest_gets_session_based_recommendations(self):
        run_training(evaluate=False)

        request = self.client.get(reverse('rec-keep-shopping')).wsgi_request
        request.user = type('Anon', (), {'is_authenticated': False})()

        products, reasons = serving.recommended_for_you(request, limit=10)
        self.assertGreater(len(products), 0, 'guest rail was empty')

    def test_recommended_for_you_never_returns_empty(self):
        response = self.client.get(reverse('rec-for-you'), {'limit': 10})
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data['results']), 0)

    def test_not_interested_filters_the_product_out(self):
        self.client.force_authenticate(self.shopper)
        hidden = self.products[0]

        response = self.client.post(reverse('rec-not-interested'), {'product_id': hidden.id})
        self.assertEqual(response.status_code, 201)

        cache.clear()
        response = self.client.get(reverse('rec-for-you'), {'limit': 50})
        self.assertNotIn(hidden.id, [row['id'] for row in response.data['results']])

    def test_guest_dismissal_does_not_duplicate(self):
        """
        Postgres treats NULLs as distinct, so the guest branch needs its own
        partial unique constraint — without it every repeat dismissal from a
        signed-out visitor inserts another row.
        """
        product = self.products[0]
        headers = {'HTTP_X_VISITOR_ID': '11111111-2222-3333-4444-555555555555'}

        for _ in range(3):
            response = self.client.post(
                reverse('rec-not-interested'), {'product_id': product.id}, **headers,
            )
            self.assertEqual(response.status_code, 201)

        self.assertEqual(
            NotInterested.objects.filter(user__isnull=True, product=product).count(), 1,
        )

    def test_not_interested_can_be_undone(self):
        self.client.force_authenticate(self.shopper)
        product = self.products[0]
        self.client.post(reverse('rec-not-interested'), {'product_id': product.id})
        self.client.delete(reverse('rec-not-interested'), {'product_id': product.id}, format='json')
        self.assertFalse(NotInterested.objects.filter(user=self.shopper, product=product).exists())

    def test_similar_endpoint_returns_both_rails(self):
        product = self.products[0]
        response = self.client.get(
            reverse('rec-similar', kwargs={'sku': product.sku, 'slug': product.slug})
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('you_might_also_like', response.data)
        self.assertIn('customers_also_bought', response.data)
        self.assertGreater(len(response.data['you_might_also_like']), 0)

    def test_similar_endpoint_404s_on_unknown_product(self):
        # Derive a SKU that provably does not exist rather than hardcoding one.
        # Product.sku is a 4-digit numeric ShortUUIDField — only 10,000 possible
        # values — so any literal has a real chance of colliding with a fixture,
        # which made this test flaky before.
        taken = set(Product.objects.values_list('sku', flat=True))
        missing = next(f"SKU{n:04d}" for n in range(10000) if f"SKU{n:04d}" not in taken)

        response = self.client.get(reverse('rec-similar', kwargs={'sku': missing, 'slug': 'nope'}))
        self.assertEqual(response.status_code, 404)

    def test_cart_addons_fall_back_to_trending_for_an_empty_cart(self):
        response = self.client.get(reverse('rec-cart-addons'))
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data['results']), 0)

    def test_currency_conversion_is_applied(self):
        response = self.client.get(reverse('rec-for-you'), {'limit': 3}, HTTP_X_CURRENCY='USD')
        self.assertEqual(response.data['currency'], 'USD')


class EventTrackingTests(CatalogMixin, RecommenderTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=3)
        cls.shopper = make_user(1)

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_batch_of_events_is_accepted(self):
        payload = [
            {'product_id': self.products[0].id, 'surface': 'todays_deals',
             'event_type': 'impression', 'position': 0},
            {'product_id': self.products[1].id, 'surface': 'todays_deals',
             'event_type': 'click', 'position': 1},
        ]
        response = self.client.post(reverse('rec-track'), payload, format='json')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['accepted'], 2)

    def test_malformed_events_are_rejected(self):
        response = self.client.post(
            reverse('rec-track'), [{'surface': 'x', 'event_type': 'nope'}], format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_ctr_report_aggregates_by_surface(self):
        from .tasks import report_surface_performance

        today = timezone.now().date()
        for n in range(10):
            RecommendationEvent.objects.create(
                product=self.products[0], surface='todays_deals',
                event_type='impression', position=n, date=today,
            )
        for _ in range(2):
            RecommendationEvent.objects.create(
                product=self.products[0], surface='todays_deals',
                event_type='click', position=0, date=today,
            )

        report = report_surface_performance()
        self.assertEqual(report['todays_deals']['impressions'], 10)
        self.assertAlmostEqual(report['todays_deals']['ctr'], 0.2)


class ApiContractTests(CatalogMixin, RecommenderTestCase):
    """
    The exact response shape the storefront reads.

    The frontend destructures these field names directly — `results[].reason`
    captions each tile, `deal_price` / `savings_percent` / `stock_remaining`
    drive the deal card, `more_from_seller` is a whole rail on the product page.
    Renaming any of them silently blanks part of the UI rather than raising
    anything, so the contract is pinned here.
    """

    CARD_FIELDS = {
        'id', 'title', 'slug', 'sku', 'image', 'price', 'old_price',
        'average_rating', 'review_count', 'vendor_name', 'sub_category_slug',
        'discount_percent', 'reason',
        # Stamped per card by apply_currency() rather than declared on the
        # serializer — the card is the only place the frontend can read which
        # currency the price is in.
        'currency',
    }
    DEAL_FIELDS = CARD_FIELDS | {
        'deal_price', 'savings_percent', 'stock_remaining', 'has_flash_sale', 'deal_score',
    }

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=4)

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_rail_envelope(self):
        response = self.client.get(reverse('rec-for-you'), {'limit': 5})
        self.assertEqual(response.status_code, 200)
        for key in ('count', 'currency', 'results'):
            self.assertIn(key, response.data)
        self.assertIsInstance(response.data['results'], list)

    def test_product_card_fields(self):
        response = self.client.get(reverse('rec-for-you'), {'limit': 3})
        card = response.data['results'][0]
        self.assertEqual(self.CARD_FIELDS, set(card.keys()))

    def test_deal_card_fields(self):
        run_deal_scoring()
        response = self.client.get(reverse('rec-todays-deals'), {'limit': 3})
        card = response.data['results'][0]
        self.assertEqual(self.DEAL_FIELDS, set(card.keys()))

    def test_similar_endpoint_returns_all_three_rails(self):
        product = self.products[0]
        response = self.client.get(
            reverse('rec-similar', kwargs={'sku': product.sku, 'slug': product.slug})
        )
        self.assertEqual(response.status_code, 200)
        for rail in ('you_might_also_like', 'customers_also_bought', 'more_from_seller'):
            self.assertIn(rail, response.data)
            self.assertIsInstance(response.data[rail], list)

    def test_more_from_seller_excludes_the_product_being_viewed(self):
        product = self.products[0]
        response = self.client.get(
            reverse('rec-similar', kwargs={'sku': product.sku, 'slug': product.slug})
        )
        ids = [row['id'] for row in response.data['more_from_seller']]
        self.assertNotIn(product.id, ids)

    def test_limit_is_capped(self):
        """A hostile or buggy client cannot ask for the whole catalog."""
        response = self.client.get(reverse('rec-for-you'), {'limit': 100000})
        self.assertLessEqual(len(response.data['results']), 60)

    def test_bad_limit_falls_back_to_the_default(self):
        response = self.client.get(reverse('rec-for-you'), {'limit': 'abc'})
        self.assertEqual(response.status_code, 200)

    def test_track_accepts_a_single_event_not_only_a_list(self):
        """The beacon path posts one object; the rail path posts an array."""
        response = self.client.post(
            reverse('rec-track'),
            {'product_id': self.products[0].id, 'surface': 'todays_deals',
             'event_type': 'impression', 'position': 0},
            format='json',
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['accepted'], 1)

    def test_deal_debug_breakdown_is_opt_in(self):
        run_deal_scoring()
        plain = self.client.get(reverse('rec-todays-deals'), {'limit': 1})
        self.assertIsNone(plain.data['results'][0]['deal_score'])

        debug = self.client.get(reverse('rec-todays-deals'), {'limit': 1, 'debug': '1'})
        breakdown = debug.data['results'][0]['deal_score']
        self.assertIsNotNone(breakdown)
        self.assertIn('discount', breakdown)
        self.assertIn('price_credibility', breakdown)


class ModelHealthTests(CatalogMixin, RecommenderTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vendor, cls.products = cls.build_catalog(per_group=3)
        cls.staff = make_user(1, is_staff=True)
        cls.shopper = make_user(2)

    def test_health_requires_staff(self):
        client = APIClient()
        client.force_authenticate(self.shopper)
        self.assertEqual(client.get(reverse('rec-health')).status_code, 403)

    def test_health_reports_metrics_after_a_run(self):
        run_training(evaluate=False)
        client = APIClient()
        client.force_authenticate(self.staff)

        response = client.get(reverse('rec-health'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['trained'])
        self.assertIn('metrics', response.data)
        self.assertIn('cf_weight', response.data)

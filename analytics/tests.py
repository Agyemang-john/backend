"""
Funnel collection, rollups, experiment assignment, purchase attribution and significance.

Run with:  DB_HOST=localhost python manage.py test analytics.tests --keepdb
"""
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from analytics import collector
from analytics.models import DailyMetric, Experiment, ExperimentExposure
from ecommerce.celery import app as celery_app
from order.models import Order
from userauths.models import User

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}
V1, V2 = 'aaaaaaaa-1111-4111-8111-111111111111', 'bbbbbbbb-2222-4222-8222-222222222222'


@override_settings(**TEST_SETTINGS)
class AnalyticsTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def collect(self, visitor, *names, client=None):
        c = client or APIClient()
        return c.post('/api/v1/analytics/collect/', {'events': [{'name': n} for n in names]}, format='json',
                      HTTP_X_VISITOR_ID=visitor)

    def test_funnel_counts_unique_visitors(self):
        self.collect(V1, 'page_view', 'page_view', 'product_view', 'add_to_cart')
        self.collect(V2, 'page_view', 'not_an_event', 'purchase')
        collector.flush()
        today = timezone.localdate()
        m = {r.name: r for r in DailyMetric.objects.filter(date=today, experiment='')}
        self.assertEqual((m['page_view'].visitors, m['page_view'].events), (2, 3))
        self.assertEqual(m['add_to_cart'].visitors, 1)
        self.assertNotIn('purchase', m, "the browser can't report purchases")
        self.assertNotIn('not_an_event', m)

    def test_bots_and_missing_visitor_ignored(self):
        self.collect(V1, 'page_view', client=APIClient(HTTP_USER_AGENT='Googlebot/2.1'))
        APIClient().post('/api/v1/analytics/collect/', {'events': [{'name': 'page_view'}]}, format='json')
        self.assertEqual(collector.flush(), 0)

    def test_assignment_is_sticky_and_weighted(self):
        variants = [{'key': 'control', 'weight': 50}, {'key': 'b', 'weight': 50}]
        picks = [collector.assign('cta', variants, f'visitor-{i:04d}') for i in range(2000)]
        self.assertTrue(900 < picks.count('b') < 1100)
        self.assertEqual(collector.assign('cta', variants, 'visitor-0001'), collector.assign('cta', variants, 'visitor-0001'))
        self.assertEqual({collector.assign('x', [{'key': 'a', 'weight': 0}, {'key': 'only', 'weight': 1}], f'v{i:07d}')
                          for i in range(50)}, {'only'})

    def test_experiment_exposure_and_purchase_conversion(self):
        exp = Experiment.objects.create(key='checkout_button', name='Bigger checkout button', status=Experiment.RUNNING,
                                        variants=[{'key': 'control', 'weight': 50}, {'key': 'big', 'weight': 50}])
        buyer = User.objects.create_user('An', 'Buyer', 'an1@example.com', '+233255000001', 'Str0ng!Passw0rd')
        User.objects.filter(pk=buyer.pk).update(is_active=True)
        api = APIClient()
        assigned = api.get('/api/v1/analytics/experiments/', HTTP_X_VISITOR_ID=V1).data
        self.assertIn(assigned['checkout_button'], ('control', 'big'))
        self.assertEqual(ExperimentExposure.objects.get().user, None)

        # Signs in later: the exposure is linked, then the order converts it.
        api.force_authenticate(buyer)
        self.collect(V1, 'page_view', client=api)
        self.assertEqual(ExperimentExposure.objects.get().user, buyer)
        with self.captureOnCommitCallbacks(execute=True):
            Order.objects.create(user=buyer, total=Decimal('10'), is_ordered=True, order_number='ORD-AN-1')
        self.assertIsNotNone(ExperimentExposure.objects.get().converted_at)
        collector.flush()
        self.assertTrue(DailyMetric.objects.filter(name='purchase', experiment='checkout_button',
                                                   variant=assigned['checkout_button']).exists())

    def test_results_significance(self):
        exp = Experiment.objects.create(key='hero', name='Hero', status=Experiment.RUNNING,
                                        variants=[{'key': 'control', 'weight': 1}, {'key': 'new', 'weight': 1}])
        now = timezone.now()
        rows = []
        for i in range(1000):
            rows.append(ExperimentExposure(experiment=exp, visitor=f'c{i:05d}', variant='control', converted_at=now if i < 50 else None))
            rows.append(ExperimentExposure(experiment=exp, visitor=f'n{i:05d}', variant='new', converted_at=now if i < 90 else None))
        ExperimentExposure.objects.bulk_create(rows)
        res = {r['variant']: r for r in collector.results(exp)}
        self.assertEqual((res['control']['rate'], res['new']['rate']), (5.0, 9.0))
        self.assertEqual(res['new']['lift'], 80.0)
        self.assertLess(res['new']['p_value'], 0.01)

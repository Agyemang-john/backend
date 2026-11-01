"""
Request IDs, health endpoint and Sentry scrubbing.

Run with:  DB_HOST=localhost python manage.py test core.tests_observability --keepdb
"""
from django.test import TestCase, override_settings

from core.observability import before_send

TEST_SETTINGS = {'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}}


@override_settings(**TEST_SETTINGS)
class ObservabilityTests(TestCase):

    def test_request_id_is_echoed_or_generated(self):
        res = self.client.get('/api/health/', HTTP_X_REQUEST_ID='abc12345-trace')
        self.assertEqual(res['X-Request-ID'], 'abc12345-trace')
        res = self.client.get('/api/health/', HTTP_X_REQUEST_ID='<script>')
        self.assertRegex(res['X-Request-ID'], r'^[0-9a-f]{32}$', "unsafe ids are replaced")

    def test_health(self):
        res = self.client.get('/api/health/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['checks']['database'], 'ok')
        from django.core.cache import cache
        from django.utils import timezone

        from core.observability import HEARTBEAT_KEY
        cache.set(HEARTBEAT_KEY, timezone.now().timestamp() - 3600)
        res = self.client.get('/api/health/')
        self.assertEqual(res.status_code, 503)
        self.assertIn('stale', res.json()['checks']['celery'])

    def test_sentry_events_carry_no_personal_data(self):
        event = {
            'request': {'cookies': {'access': 'jwt'}, 'data': {'address': '12 Ring Rd'},
                        'headers': {'Authorization': 'Bearer x', 'User-Agent': 'UA', 'X-Forwarded-For': '1.2.3.4'},
                        'query_string': 'token=abc&page=2'},
            'user': {'id': 7, 'email': 'a@b.c', 'ip_address': '1.2.3.4'},
            'exception': {'values': [{'stacktrace': {'frames': [{'vars': {'password': 'p', 'n': 1}}]}}]},
        }
        out = before_send(event, {})
        self.assertNotIn('cookies', out['request'])
        self.assertNotIn('data', out['request'])
        self.assertEqual(out['request']['headers']['Authorization'], '[Filtered]')
        self.assertEqual(out['request']['headers']['X-Forwarded-For'], '[Filtered]')
        self.assertEqual(out['request']['headers']['User-Agent'], 'UA')
        self.assertEqual(out['request']['query_string'], 'token=[Filtered]&page=2')
        self.assertEqual(out['user'], {'id': 7})
        self.assertEqual(out['exception']['values'][0]['stacktrace']['frames'][0]['vars'], {'password': '[Filtered]', 'n': 1})


@override_settings(**TEST_SETTINGS)
class LanguageTests(TestCase):

    def test_api_answers_in_the_shoppers_language(self):
        from rest_framework.test import APIClient
        # DRF's own messages are translated; ours fall back to English until translated.
        res = APIClient().get('/api/v1/engagement/alerts/', HTTP_ACCEPT_LANGUAGE='fr')
        self.assertEqual(res.status_code, 401)
        self.assertIn("Informations d'authentification non fournies", res.json()['detail'])
        res = APIClient().get('/api/v1/engagement/alerts/')
        self.assertIn('Authentication credentials were not provided', res.json()['detail'])

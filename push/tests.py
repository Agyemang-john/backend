"""
Push notifications: subscribe, app routing, dead endpoints, hourly cap.

Run with:  DB_HOST=localhost python manage.py test push.tests --keepdb
"""
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from notification.utils import send_notification
from push.models import PushSubscription
from userauths.models import User

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
    'WEBPUSH_VAPID_PUBLIC_KEY': 'test-public', 'WEBPUSH_VAPID_PRIVATE_KEY': 'test-private',
    'PUSH_HOURLY_CAP': 3,
}
SUB = {'endpoint': 'https://push.example.com/abc', 'keys': {'p256dh': 'BPk', 'auth': 'xyz'}}


@override_settings(**TEST_SETTINGS)
class PushTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('Push', 'User', 'push1@example.com', '+233253000001', 'Str0ng!Passw0rd')
        User.objects.filter(pk=self.user.pk).update(is_active=True, email_verified_at=timezone.now())
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def subscribe(self, app='shop', endpoint=SUB['endpoint']):
        return self.api.post('/api/v1/push/subscriptions/', {**SUB, 'endpoint': endpoint, 'app': app}, format='json')

    def test_config_exposes_public_key_only(self):
        res = APIClient().get('/api/v1/push/config/')
        self.assertEqual(res.data['vapid_public_key'], 'test-public')
        self.assertNotIn('test-private', str(res.content))

    def test_subscribe_is_idempotent_and_shop_only_for_shoppers(self):
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(self.subscribe().status_code, 201)
        self.assertEqual(PushSubscription.objects.count(), 1)
        self.assertEqual(self.subscribe(app='seller').status_code, 403, "no store, no seller pushes")
        self.assertEqual(self.api.post('/api/v1/push/subscriptions/', {'app': 'shop', 'endpoint': 'http://x.y/z',
                                                                       'keys': SUB['keys']}, format='json').status_code, 400)

    @mock.patch('pywebpush.webpush')
    def test_notification_is_pushed_to_the_right_app(self, webpush):
        self.subscribe()
        with self.captureOnCommitCallbacks(execute=True):
            send_notification(self.user, 'customer_order_shipped', data={'message': 'On its way', 'url': '/dashboard/order-history/1'})
        self.assertEqual(webpush.call_count, 1)
        payload = webpush.call_args.kwargs['data']
        self.assertIn('On its way', payload)
        self.assertIn('/dashboard/order-history/1', payload)
        with self.captureOnCommitCallbacks(execute=True):
            send_notification(self.user, 'vendor_new_order', data={'message': 'seller only'})
        self.assertEqual(webpush.call_count, 1, "seller notifications don't go to the shop app")

    @mock.patch('pywebpush.webpush')
    def test_gone_endpoint_is_deleted(self, webpush):
        from pywebpush import WebPushException
        self.subscribe()
        response = mock.Mock(status_code=410)
        webpush.side_effect = WebPushException('gone', response=response)
        with self.captureOnCommitCallbacks(execute=True):
            send_notification(self.user, 'customer_order_shipped', data={'message': 'x'})
        self.assertFalse(PushSubscription.objects.exists())

    @mock.patch('pywebpush.webpush')
    def test_hourly_cap(self, webpush):
        self.subscribe()
        for _ in range(5):
            with self.captureOnCommitCallbacks(execute=True):
                send_notification(self.user, 'customer_order_shipped', data={'message': 'x'})
        self.assertEqual(webpush.call_count, 3)

    def test_unsubscribe(self):
        self.subscribe()
        self.api.delete('/api/v1/push/subscriptions/', {'endpoint': SUB['endpoint']}, format='json')
        self.assertFalse(PushSubscription.objects.exists())

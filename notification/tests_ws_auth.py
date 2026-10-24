"""
notification/tests_ws_auth.py
WebSocket tickets: one use, short-lived, and no stronger than the session that
asked for them.

Run with:  DB_HOST=localhost python manage.py test notification.tests_ws_auth --keepdb
"""

from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from userauths.models import User
from userauths.session_utils import REVOKED_SID_PREFIX

from .middleware import get_user_from_ticket, issue_ticket

TEST_SETTINGS = {'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}}


@override_settings(**TEST_SETTINGS)
class WebSocketTicketTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('W', 'S', 'ws@example.com', '+233200000077', 'Str0ng!Passw0rd')
        User.objects.filter(pk=self.user.pk).update(is_active=True, email_verified_at=timezone.now())
        self.user.refresh_from_db()

    def redeem(self, ticket):
        return async_to_sync(get_user_from_ticket)(ticket)

    def ticket(self, scope='shopper', sid=None):
        self.user.__dict__['_auth_scope'] = scope
        return issue_ticket(SimpleNamespace(user=self.user, auth={'sid': sid} if sid else {}))

    def test_endpoint_returns_a_ticket_not_the_access_token(self):
        access = str(RefreshToken.for_user(self.user).access_token)
        client = APIClient()
        client.cookies['access'] = access
        token = client.get('/api/v1/notification/ws-token/').data['token']
        self.assertNotEqual(token, access)
        self.assertNotIn('.', token)    # not a JWT
        self.assertEqual(self.redeem(token).pk, self.user.pk)

    def test_ticket_works_once(self):
        ticket = self.ticket()
        self.assertEqual(self.redeem(ticket).pk, self.user.pk)
        self.assertTrue(self.redeem(ticket).is_anonymous)

    def test_jwt_is_not_accepted_on_the_socket(self):
        self.assertTrue(self.redeem(str(RefreshToken.for_user(self.user))).is_anonymous)
        self.assertTrue(self.redeem(str(RefreshToken.for_user(self.user).access_token)).is_anonymous)

    def test_scope_travels_with_the_ticket(self):
        self.assertEqual(self.redeem(self.ticket('shopper')).__dict__['_auth_scope'], 'shopper')
        self.assertEqual(self.redeem(self.ticket('seller')).__dict__['_auth_scope'], 'seller')

    def test_signed_out_everywhere_or_device_revoked(self):
        ticket = self.ticket()
        User.objects.filter(pk=self.user.pk).update(token_version=self.user.token_version + 1)
        self.assertTrue(self.redeem(ticket).is_anonymous)

        self.user.refresh_from_db()
        ticket = self.ticket('seller', sid='device-1')
        cache.set(REVOKED_SID_PREFIX + 'device-1', 1, 60)
        self.assertTrue(self.redeem(ticket).is_anonymous)

    def test_inactive_user(self):
        ticket = self.ticket()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertTrue(self.redeem(ticket).is_anonymous)

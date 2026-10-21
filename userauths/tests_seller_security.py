"""
userauths/tests_seller_security.py
Seller account security: signing devices out for real, password changes,
the security overview and new-device sign-in alerts.

Run with:  DB_HOST=localhost python manage.py test userauths.tests_seller_security --keepdb
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from vendor.models import Vendor

from .models import OTPRecord, SecurityEvent, User, UserSession
from .session_utils import register_session
from .tokens import CustomVendorRefreshToken

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

PASSWORD = 'Str0ng!Passw0rd'
CHROME_MAC = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
FIREFOX_WIN = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0'

SECURITY_URL = '/api/vendor/security/'
SESSIONS_URL = '/api/vendor/sessions/'
VENDOR_REFRESH_URL = '/api/jwt/refresh/vendor/'


@override_settings(**TEST_SETTINGS)
class SellerSecurityTests(TestCase):

    def setUp(self):
        cache.clear()
        for target in ('vendor.models.send_vendor_approval_email.delay',
                       'vendor.models.send_vendor_sms.delay',
                       'vendor.tasks.log_vendor_activity.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

        self.user = User.objects.create_user('Ama', 'Mensah', 'ama@example.com', '+233200000001', PASSWORD)
        User.objects.filter(pk=self.user.pk).update(is_active=True, email_verified_at=timezone.now())
        self.user.refresh_from_db()
        Vendor.objects.create(
            name='Secure Shop', user=self.user, email='shop@example.com',
            contact='+233550001111', is_approved=True, status='VERIFIED',
        )

    # ── helpers ──

    def seller_device(self, ua=CHROME_MAC):
        """Sign in on a new 'device' the way VendorOTPVerifyView does."""
        refresh = CustomVendorRefreshToken.for_user(self.user)
        request = APIClient().get('/', HTTP_USER_AGENT=ua).wsgi_request
        register_session(self.user, refresh['jti'], request, is_vendor=True)
        client = APIClient(HTTP_USER_AGENT=ua)
        client.cookies['vendor_access'] = str(refresh.access_token)
        client.cookies['vendor_refresh'] = str(refresh)
        client.credentials(HTTP_X_USER_TYPE='vendor')
        return client, UserSession.objects.get(session_key=refresh['jti'])

    def shopper_session(self):
        refresh = RefreshToken.for_user(self.user)
        refresh['token_version'] = self.user.token_version
        request = APIClient().get('/').wsgi_request
        register_session(self.user, refresh['jti'], request, is_vendor=False)
        return str(refresh), UserSession.objects.get(session_key=refresh['jti'])

    # ── signing devices out ──

    def test_signed_out_device_loses_access_immediately(self):
        me, _ = self.seller_device()
        thief, thief_session = self.seller_device(FIREFOX_WIN)
        self.assertEqual(thief.get(SECURITY_URL).status_code, 200)

        res = me.delete(f'{SESSIONS_URL}{thief_session.id}/')
        self.assertEqual(res.status_code, 204)

        # Its still-unexpired access token is rejected straight away...
        self.assertEqual(thief.get(SECURITY_URL).status_code, 401)
        # ...and its refresh token cannot mint a new one.
        self.assertIn(thief.post(VENDOR_REFRESH_URL, {}, format='json').status_code, (400, 401))
        # The device doing the signing out is unaffected.
        self.assertEqual(me.get(SECURITY_URL).status_code, 200)

    def test_revoked_device_cannot_sneak_back_once_no_sessions_remain(self):
        # Before: with zero session rows left the refresh view re-adopted any token.
        me, _ = self.seller_device()
        thief, thief_session = self.seller_device(FIREFOX_WIN)
        me.delete(f'{SESSIONS_URL}{thief_session.id}/')
        me.post('/api/vendor/logout/')
        self.assertFalse(UserSession.objects.filter(user=self.user, is_vendor_session=True).exists())

        cache.clear()  # even with the revocation marks gone
        self.assertIn(thief.post(VENDOR_REFRESH_URL, {}, format='json').status_code, (400, 401))
        self.assertFalse(UserSession.objects.filter(user=self.user, is_vendor_session=True).exists())

    def test_verify_reports_signed_out_device(self):
        # The dashboard's periodic verify must notice, so it bounces to login.
        me, _ = self.seller_device()
        thief, thief_session = self.seller_device(FIREFOX_WIN)
        self.assertEqual(thief.post('/api/jwt/verify/vendor/', {}, format='json').status_code, 200)
        me.delete(f'{SESSIONS_URL}{thief_session.id}/')
        self.assertEqual(thief.post('/api/jwt/verify/vendor/', {}, format='json').status_code, 401)

    def test_logout_kills_the_refresh_token(self):
        me, _ = self.seller_device()
        stolen_refresh = me.cookies['vendor_refresh'].value
        me.post('/api/vendor/logout/')

        replay = APIClient()
        replay.cookies['vendor_refresh'] = stolen_refresh
        self.assertIn(replay.post(VENDOR_REFRESH_URL, {}, format='json').status_code, (400, 401))
        self.assertTrue(SecurityEvent.objects.filter(user=self.user, event=SecurityEvent.SIGN_OUT).exists())

    def test_refresh_keeps_the_session_id_on_new_access_tokens(self):
        me, session = self.seller_device()
        res = me.post(VENDOR_REFRESH_URL, {}, format='json')
        self.assertEqual(res.status_code, 200)
        from rest_framework_simplejwt.tokens import AccessToken
        self.assertEqual(AccessToken(res.cookies['vendor_access'].value)['sid'], session.session_key)

    def test_sign_out_other_devices_covers_shop_sessions_but_keeps_this_browser(self):
        me, mine = self.seller_device()
        my_shop_refresh, my_shop = self.shopper_session()
        me.cookies['refresh'] = my_shop_refresh
        self.seller_device(FIREFOX_WIN)
        self.shopper_session()

        res = me.post('/api/vendor/logout-other-sessions/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['count'], 2)
        self.assertEqual(
            set(UserSession.objects.filter(user=self.user).values_list('pk', flat=True)),
            {mine.pk, my_shop.pk},
        )

    def test_sessions_list_marks_kind_and_current(self):
        me, mine = self.seller_device()
        self.shopper_session()
        rows = me.get(SESSIONS_URL).data
        self.assertEqual([r['kind'] for r in rows], ['seller', 'shopper'])
        self.assertTrue(next(r for r in rows if r['id'] == str(mine.id))['is_current'])

    def test_cannot_revoke_current_device(self):
        me, mine = self.seller_device()
        self.assertEqual(me.delete(f'{SESSIONS_URL}{mine.id}/').status_code, 400)

    def test_cannot_revoke_someone_elses_session(self):
        me, _ = self.seller_device()
        other = User.objects.create_user('Kofi', 'Owusu', 'kofi@example.com', '+233200000002', PASSWORD)
        refresh = RefreshToken.for_user(other)
        register_session(other, refresh['jti'], APIClient().get('/').wsgi_request)
        theirs = UserSession.objects.get(user=other)
        self.assertEqual(me.delete(f'{SESSIONS_URL}{theirs.id}/').status_code, 404)
        self.assertTrue(UserSession.objects.filter(pk=theirs.pk).exists())

    def test_sign_out_everywhere(self):
        me, _ = self.seller_device()
        thief, _ = self.seller_device(FIREFOX_WIN)
        res = me.post('/api/vendor/logout-all/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(thief.get(SECURITY_URL).status_code, 401)
        self.assertEqual(me.get(SECURITY_URL).status_code, 401)
        self.assertFalse(UserSession.objects.filter(user=self.user).exists())

    # ── password ──

    def test_change_password_signs_out_other_devices(self):
        me, _ = self.seller_device()
        thief, _ = self.seller_device(FIREFOX_WIN)
        res = me.post('/api/vendor/security/password/', {
            'current_password': PASSWORD, 'new_password': 'An0ther!Passw0rd',
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['signed_out_devices'], 1)
        self.assertEqual(thief.get(SECURITY_URL).status_code, 401)
        self.assertEqual(me.get(SECURITY_URL).status_code, 200)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('An0ther!Passw0rd'))
        self.assertIsNotNone(self.user.password_changed_at)

    def test_change_password_can_keep_other_devices(self):
        me, _ = self.seller_device()
        other, _ = self.seller_device(FIREFOX_WIN)
        res = me.post('/api/vendor/security/password/', {
            'current_password': PASSWORD, 'new_password': 'An0ther!Passw0rd',
            'sign_out_other_devices': False,
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(other.get(SECURITY_URL).status_code, 200)

    def test_change_password_rejects_wrong_current_and_weak_new(self):
        me, _ = self.seller_device()
        bad = me.post('/api/vendor/security/password/', {
            'current_password': 'nope', 'new_password': 'An0ther!Passw0rd',
        }, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertIn('current_password', bad.data)

        weak = me.post('/api/vendor/security/password/', {
            'current_password': PASSWORD, 'new_password': '123',
        }, format='json')
        self.assertEqual(weak.status_code, 400)
        self.assertIn('new_password', weak.data)

    # ── overview & alerts ──

    def test_overview(self):
        me, _ = self.seller_device()
        self.shopper_session()
        data = me.get(SECURITY_URL).data
        self.assertTrue(data['email_verified'])
        self.assertTrue(data['two_step_sign_in'])
        self.assertEqual(data['seller_devices'], 1)
        self.assertEqual(data['shopper_devices'], 1)
        self.assertEqual(data['store_role'], 'owner')
        self.assertTrue(data['phone'].endswith('001'))
        self.assertNotIn('+233200000001', data['phone'])

    def _otp_sign_in(self, ua):
        OTPRecord.create_for_user(self.user, 12345)
        client = APIClient(HTTP_USER_AGENT=ua)
        client.cookies['otp_identifier'] = self.user.email
        with mock.patch('userauths.tasks.send_new_sign_in_alert.delay') as alert:
            res = client.post('/api/jwt/otp-verify/vendor/', {'otp': '12345'}, format='json')
        self.assertEqual(res.status_code, 200)
        return alert

    def test_new_device_sign_in_sends_alert(self):
        self.assertFalse(self._otp_sign_in(CHROME_MAC).called)    # first ever: nothing to compare
        self.assertFalse(self._otp_sign_in(CHROME_MAC).called)    # same device again
        alert = self._otp_sign_in(FIREFOX_WIN)                    # new device
        self.assertTrue(alert.called)
        event = SecurityEvent.objects.get(pk=alert.call_args[0][0])
        self.assertTrue(event.new_device)
        self.assertEqual(event.event, SecurityEvent.SIGN_IN)

    def test_alert_email_renders(self):
        from django.core import mail
        from .session_utils import record_security_event
        request = APIClient().get('/', HTTP_USER_AGENT=FIREFOX_WIN).wsgi_request
        event = record_security_event(self.user, SecurityEvent.SIGN_IN, request, new_device=True)
        from .tasks import send_new_sign_in_alert
        send_new_sign_in_alert(event.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Firefox', mail.outbox[0].alternatives[0][0])
        self.assertIn('settings?tab=security', mail.outbox[0].body)

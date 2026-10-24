"""
userauths/tests_auth_hardening.py
Attacks on seller sign-in that must fail:

  - pointing the OTP step at someone else's account with a hand-made cookie
  - guessing the 5-digit code (capped per code and per sign-in)
  - skipping the code with a shopper token (password only)
  - dodging rate limits with a made-up X-Forwarded-For header

Run with:  DB_HOST=localhost python manage.py test userauths.tests_auth_hardening --keepdb
"""

from unittest import mock

from django.core import signing
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.client_ip import client_ip
from vendor.models import Vendor

from .models import OTPRecord, User
from .tokens import CustomVendorRefreshToken
from .verification import MAX_ATTEMPTS
from .views import VENDOR_LOGIN_COOKIE, VENDOR_LOGIN_SALT

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

PASSWORD = 'Str0ng!Passw0rd'
LOGIN_URL = '/api/jwt/create/vendor/'
VERIFY_URL = '/api/jwt/otp-verify/vendor/'
RESEND_URL = '/api/jwt/otp-resend/vendor/'
VENDOR_REFRESH_URL = '/api/jwt/refresh/vendor/'
TEAM_URL = '/api/v1/vendor/team/'
SECURITY_URL = '/api/vendor/security/'


@override_settings(**TEST_SETTINGS)
class SellerSignInHardeningTests(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(mock.patch.stopall)
        for target in ('vendor.models.send_vendor_approval_email.delay',
                       'vendor.models.send_vendor_sms.delay',
                       'vendor.tasks.log_vendor_activity.delay',
                       'userauths.tasks.send_new_sign_in_alert.delay',
                       'userauths.captcha.verify_turnstile'):
            mock.patch(target, return_value=True).start()
        self.sent = mock.patch('userauths.tasks.send_otp.delay').start()
        # Per-IP throttles are a second layer; these tests check the caps alone.
        mock.patch('rest_framework.throttling.SimpleRateThrottle.allow_request', return_value=True).start()

        self.user = self._seller('ama@example.com', '+233200000001', 'Ama Shop')
        self.victim = self._seller('kofi@example.com', '+233200000002', 'Kofi Shop')

    def _seller(self, email, phone, shop):
        user = User.objects.create_user('A', 'B', email, phone, PASSWORD)
        User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
        user.refresh_from_db()
        Vendor.objects.create(name=shop, user=user, email=f'shop.{email}', contact=phone[:-1] + '9',
                              is_approved=True, status='VERIFIED')
        return user

    def _login(self, client=None, user=None):
        client = client or APIClient()
        user = user or self.user
        res = client.post(LOGIN_URL, {'email': user.email, 'password': PASSWORD,
                                      'cf_turnstile_response': 'x'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        return client

    def _set_code(self, user, code=12345):
        OTPRecord.create_for_user(user, code)

    # ── the OTP step trusts only the signed cookie ──

    def test_login_cookie_is_signed_not_an_email(self):
        client = self._login()
        value = client.cookies[VENDOR_LOGIN_COOKIE].value
        self.assertNotIn(self.user.email, value)
        self.assertEqual(signing.loads(value, salt=VENDOR_LOGIN_SALT)['uid'], self.user.pk)

    def test_plain_email_cookie_cannot_target_another_seller(self):
        self._set_code(self.victim)
        attacker = APIClient()
        attacker.cookies[VENDOR_LOGIN_COOKIE] = self.victim.email
        res = attacker.post(VERIFY_URL, {'otp': '12345'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertNotIn('vendor_access', res.cookies)
        # Resend without the password step is refused too, and sends nothing.
        self.sent.reset_mock()
        self.assertEqual(attacker.post(RESEND_URL).status_code, 400)
        self.sent.assert_not_called()

    def test_tampered_cookie_is_rejected(self):
        client = self._login()
        value = client.cookies[VENDOR_LOGIN_COOKIE].value
        forged = signing.dumps({'uid': self.victim.pk, 'ch': 'email', 'pw': 'x'}, salt='wrong')
        for bad in (value[:-2] + 'zz', forged):
            other = APIClient()
            other.cookies[VENDOR_LOGIN_COOKIE] = bad
            self._set_code(self.victim)
            self.assertEqual(other.post(VERIFY_URL, {'otp': '12345'}, format='json').status_code, 400)

    def test_correct_code_signs_in(self):
        client = self._login()
        self._set_code(self.user)
        res = client.post(VERIFY_URL, {'otp': '12345'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertIn('vendor_access', res.cookies)

    def test_wrong_guesses_burn_the_code(self):
        client = self._login()
        self._set_code(self.user)
        for _ in range(MAX_ATTEMPTS):
            client.post(VERIFY_URL, {'otp': '00000'}, format='json')
        res = client.post(VERIFY_URL, {'otp': '12345'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertNotIn('vendor_access', res.cookies)

    def test_resends_are_capped_per_sign_in(self):
        client = self._login()
        codes = 0
        for _ in range(10):
            OTPRecord.objects.filter(user=self.user).update(
                created_at=timezone.now() - timezone.timedelta(minutes=2))   # skip the 60s cooldown
            if client.post(RESEND_URL).status_code == 200:
                codes += 1
        self.assertLessEqual(codes, 5)

    def test_password_change_kills_pending_sign_in(self):
        client = self._login()
        self.user.set_password('An0ther!Passw0rd')
        self.user.save()
        self._set_code(self.user)
        self.assertEqual(client.post(VERIFY_URL, {'otp': '12345'}, format='json').status_code, 400)

    def test_expired_login_cookie_is_rejected(self):
        client = self._login()
        self._set_code(self.user)
        with mock.patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 3600):
            self.assertEqual(client.post(VERIFY_URL, {'otp': '12345'}, format='json').status_code, 400)

    # ── shopper tokens never reach the dashboard ──

    def _shopper_token(self, user=None):
        refresh = RefreshToken.for_user(user or self.user)
        refresh['token_version'] = (user or self.user).token_version
        return refresh

    def test_shopper_token_in_vendor_cookie_is_refused(self):
        client = APIClient()
        client.cookies['vendor_access'] = str(self._shopper_token().access_token)
        client.credentials(HTTP_X_USER_TYPE='vendor')
        self.assertEqual(client.get(TEAM_URL).status_code, 401)

    def test_shopper_token_without_vendor_header_has_no_store_access(self):
        token = str(self._shopper_token().access_token)
        bearer = APIClient()
        bearer.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        cookie = APIClient()
        cookie.cookies['access'] = token
        for client in (bearer, cookie):
            self.assertEqual(client.get(TEAM_URL).status_code, 403)
            self.assertEqual(client.get(SECURITY_URL).status_code, 403)
            self.assertEqual(client.get('/api/v1/payments/billing/overview/').status_code, 403)

    def test_shopper_refresh_cannot_mint_seller_session(self):
        client = APIClient()
        client.cookies['vendor_refresh'] = str(self._shopper_token())
        res = client.post(VENDOR_REFRESH_URL, {}, format='json')
        self.assertIn(res.status_code, (400, 401))
        self.assertNotIn('vendor_access', res.cookies)

    def test_seller_token_still_works(self):
        refresh = CustomVendorRefreshToken.for_user(self.user)
        from .session_utils import register_session
        register_session(self.user, refresh['jti'], APIClient().get('/').wsgi_request, is_vendor=True)
        client = APIClient()
        client.cookies['vendor_access'] = str(refresh.access_token)
        client.credentials(HTTP_X_USER_TYPE='vendor')
        self.assertEqual(client.get(TEAM_URL).status_code, 200)
        self.assertEqual(client.get(SECURITY_URL).status_code, 200)

    def test_shopper_token_still_knows_own_store_for_status(self):
        client = APIClient()
        client.cookies['access'] = str(self._shopper_token().access_token)
        client.credentials(HTTP_X_USER_TYPE='customer')
        data = client.get('/api/v1/vendor/my-status/').data
        self.assertTrue(data['is_vendor'])
        self.assertTrue(data['can_access_dashboard'])


class ClientIpTests(TestCase):

    def test_spoofed_forwarded_for_is_ignored(self):
        # nginx overwrites the header with the address it saw; if an old config
        # appends instead, only the right-most entry (added by nginx) counts.
        request = RequestFactory().get('/', HTTP_X_FORWARDED_FOR='1.2.3.4, 203.0.113.9',
                                       REMOTE_ADDR='172.18.0.5')
        self.assertEqual(client_ip(request), '203.0.113.9')

    def test_no_header_uses_remote_addr(self):
        request = RequestFactory().get('/', REMOTE_ADDR='203.0.113.9')
        self.assertEqual(client_ip(request), '203.0.113.9')

    def test_garbage_header_falls_back(self):
        request = RequestFactory().get('/', HTTP_X_FORWARDED_FOR='not-an-ip', REMOTE_ADDR='203.0.113.9')
        self.assertEqual(client_ip(request), '203.0.113.9')

    def test_drf_throttle_uses_the_proxy_entry(self):
        from rest_framework.throttling import AnonRateThrottle
        throttle = AnonRateThrottle()
        a = RequestFactory().get('/', HTTP_X_FORWARDED_FOR='1.1.1.1, 203.0.113.9', REMOTE_ADDR='172.18.0.5')
        b = RequestFactory().get('/', HTTP_X_FORWARDED_FOR='2.2.2.2, 203.0.113.9', REMOTE_ADDR='172.18.0.5')
        self.assertEqual(throttle.get_ident(a), throttle.get_ident(b))


@override_settings(**TEST_SETTINGS)
class CookieOriginTests(TestCase):
    """Cookie-authenticated writes must come from our own sites (CSRF)."""

    URL = '/api/v1/notification/mark-all-read/'

    def setUp(self):
        cache.clear()
        user = User.objects.create_user('C', 'S', 'csrf@example.com', '+233200000088', PASSWORD)
        User.objects.filter(pk=user.pk).update(is_active=True)
        refresh = RefreshToken.for_user(user)
        refresh['token_version'] = user.token_version
        self.token = str(refresh.access_token)

    def cookie_client(self):
        client = APIClient()
        client.cookies['access'] = self.token
        return client

    def test_other_site_is_blocked(self):
        res = self.cookie_client().post(self.URL, HTTP_ORIGIN='https://evil.example')
        self.assertEqual(res.status_code, 403)
        res = self.cookie_client().post(self.URL, HTTP_REFERER='https://evil.example/page')
        self.assertEqual(res.status_code, 403)
        res = self.cookie_client().post(self.URL, HTTP_ORIGIN='null')
        self.assertEqual(res.status_code, 403)

    def test_our_sites_and_servers_are_allowed(self):
        self.assertNotEqual(self.cookie_client().post(self.URL, HTTP_ORIGIN='https://www.negromart.com').status_code, 403)
        # No Origin/Referer: a server (our SSR), not a victim's browser.
        self.assertNotEqual(self.cookie_client().post(self.URL).status_code, 403)

    def test_reads_and_header_tokens_are_not_affected(self):
        self.assertEqual(self.cookie_client().get('/api/v1/notification/list/', HTTP_ORIGIN='https://evil.example').status_code, 200)
        bearer = APIClient()
        bearer.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')
        self.assertNotEqual(bearer.post(self.URL, HTTP_ORIGIN='https://evil.example').status_code, 403)

    def test_auth_cookies_are_samesite_lax(self):
        from django.conf import settings
        self.assertEqual(settings.AUTH_COOKIE_SAMESITE, 'Lax')
        self.assertEqual(settings.VENDOR_AUTH_COOKIE_SAMESITE, 'Lax')
        self.assertNotIn('digitaloceanspaces', ' '.join(settings.CORS_ALLOWED_ORIGINS))

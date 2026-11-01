"""
userauths/tests_mobile_auth.py
Native-app sign-in (mobile_views.py): tokens in the body, never cookies, and
every access token tied to its device session so signing out takes effect now.

Run with:  DB_HOST=localhost python manage.py test userauths.tests_mobile_auth --keepdb
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, UntypedToken

from .models import User, UserSession

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

PASSWORD = "Str0ng!pass"
LOGIN_URL = "/api/mobile/auth/login/"
REFRESH_URL = "/api/mobile/auth/refresh/"
LOGOUT_URL = "/api/mobile/auth/logout/"
ME_URL = "/api/users/me/"
SESSIONS_URL = "/api/sessions/"


@override_settings(CACHES=LOCMEM_CACHE)
class MobileAuthTests(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(mock.patch.stopall)
        self.captcha = mock.patch("userauths.captcha.verify_turnstile", return_value=True).start()
        mock.patch("rest_framework.throttling.SimpleRateThrottle.allow_request", return_value=True).start()

        self.user = User.objects.create_user(
            first_name="Ama", last_name="Mensah",
            email="ama@example.com", phone="0240000001", password=PASSWORD,
        )
        self.user.is_active = True
        self.user.save()
        self.client = APIClient()

    def _login(self, **extra):
        return self.client.post(LOGIN_URL, {"email": "ama@example.com", "password": PASSWORD, **extra}, format="json")

    def _bearer(self, access):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client

    # ── login ────────────────────────────────────────────────────────────

    def test_login_returns_tokens_and_user_without_cookies(self):
        res = self._login()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["access"])
        self.assertTrue(res.data["refresh"])
        self.assertEqual(res.data["access_expires_in"], 3600)
        self.assertEqual(res.data["user"]["email"], "ama@example.com")
        self.assertNotIn("access", res.cookies)
        self.assertNotIn("refresh", res.cookies)

    def test_access_token_names_its_session(self):
        res = self._login(device_name="Pixel 8", os="Android 15", app_version="1.2.0")
        jti = UntypedToken(res.data["refresh"])["jti"]
        self.assertEqual(AccessToken(res.data["access"])["sid"], jti)

        session = UserSession.objects.get(session_key=jti)
        self.assertEqual(session.device_type, "mobile")
        self.assertEqual(session.device_name, "Pixel 8")
        self.assertEqual(session.os, "Android 15")
        self.assertEqual(session.browser, "Negromart app 1.2.0")

    def test_access_token_authenticates(self):
        access = self._login().data["access"]
        res = self._bearer(access).get(ME_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["email"], "ama@example.com")

    def test_mobile_token_is_not_a_seller_token(self):
        access = self._login().data["access"]
        token = AccessToken(access)
        self.assertNotIn("scope", token.payload)
        self.assertNotIn("is_vendor", token.payload)

    def test_wrong_password_is_rejected(self):
        res = self._login(password="nope")
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("access", res.data)

    def test_failed_captcha_is_rejected(self):
        self.captcha.return_value = False
        res = self._login()
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("access", res.data)

    @override_settings(MOBILE_AUTH_REQUIRE_CAPTCHA=False)
    def test_captcha_can_be_turned_off(self):
        self.captcha.return_value = False
        self.assertEqual(self._login().status_code, 200)

    # ── refresh ──────────────────────────────────────────────────────────

    def test_refresh_rotates_and_follows_the_session(self):
        first = self._login().data
        res = self.client.post(REFRESH_URL, {"refresh": first["refresh"]}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotEqual(res.data["refresh"], first["refresh"])

        new_jti = UntypedToken(res.data["refresh"])["jti"]
        self.assertEqual(AccessToken(res.data["access"])["sid"], new_jti)
        self.assertTrue(UserSession.objects.filter(session_key=new_jti).exists())
        self.assertNotIn("access", res.cookies)

    def test_refresh_ignores_the_refresh_cookie(self):
        refresh = self._login().data["refresh"]
        client = APIClient()
        client.cookies["refresh"] = refresh
        res = client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertNotIn("access", res.data)

    def test_garbage_refresh_is_401(self):
        res = self.client.post(REFRESH_URL, {"refresh": "not-a-token"}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.data["code"], "session_expired")

    # ── logout ───────────────────────────────────────────────────────────

    def test_logout_cuts_off_the_access_token_now(self):
        tokens = self._login().data
        res = self.client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(res.status_code, 204)

        self.assertEqual(self._bearer(tokens["access"]).get(ME_URL).status_code, 401)
        res = self.client.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertFalse(UserSession.objects.filter(user=self.user).exists())

    def test_logout_with_dead_token_is_quiet(self):
        res = self.client.post(LOGOUT_URL, {"refresh": "not-a-token"}, format="json")
        self.assertEqual(res.status_code, 204)

    def test_logout_leaves_other_devices_signed_in(self):
        phone = self._login().data
        tablet = self._login().data
        self.client.post(LOGOUT_URL, {"refresh": phone["refresh"]}, format="json")
        self.assertEqual(self._bearer(tablet["access"]).get(ME_URL).status_code, 200)

    # ── sessions ─────────────────────────────────────────────────────────

    def test_session_list_marks_this_device_current(self):
        self._login()
        access = self._login().data["access"]
        sessions = self._bearer(access).get(SESSIONS_URL).data
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(s["is_current"] for s in sessions), 1)

    def test_revoking_another_device_cuts_it_off(self):
        other = self._login().data
        mine = self._login().data
        other_id = UserSession.objects.get(session_key=UntypedToken(other["refresh"])["jti"]).id

        res = self._bearer(mine["access"]).delete(f"{SESSIONS_URL}{other_id}/")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(self._bearer(other["access"]).get(ME_URL).status_code, 401)

    def test_logout_all_signs_the_app_out(self):
        tokens = self._login().data
        self._bearer(tokens["access"]).post("/api/logout-all/")
        self.assertEqual(self._bearer(tokens["access"]).get(ME_URL).status_code, 401)

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User, UserSession
from .session_utils import register_session


LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
REFRESH_URL = "/api/jwt/refresh/"


def _login(user, request_factory_client):
    """Issue a refresh token + device session the way CustomTokenObtainPairView does."""
    refresh = RefreshToken.for_user(user)
    refresh["role"] = user.role
    refresh["token_version"] = user.token_version
    request = request_factory_client.get("/").wsgi_request
    register_session(user, refresh["jti"], request, is_vendor=False)
    return str(refresh)


@override_settings(CACHES=LOCMEM_CACHE)
class CustomerRefreshTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            first_name="Ama", last_name="Mensah",
            email="ama@example.com", phone="0240000001", password="Str0ng!pass",
        )
        self.user.is_active = True
        self.user.save()

    def _client_with(self, refresh):
        client = APIClient()
        client.cookies["refresh"] = refresh
        return client

    def test_refresh_rotates_and_sets_both_cookies(self):
        refresh = _login(self.user, APIClient())
        res = self._client_with(refresh).post(REFRESH_URL, {}, format="json")

        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.cookies)
        self.assertIn("refresh", res.cookies)
        self.assertNotEqual(res.cookies["refresh"].value, refresh)

    def test_chained_refreshes_keep_session_alive(self):
        refresh = _login(self.user, APIClient())
        for _ in range(3):
            res = self._client_with(refresh).post(REFRESH_URL, {}, format="json")
            self.assertEqual(res.status_code, 200)
            refresh = res.cookies["refresh"].value
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)

    def test_reusing_old_token_within_grace_returns_same_pair(self):
        # e.g. two tabs, or the server render and the browser refreshing at once
        refresh = _login(self.user, APIClient())
        first = self._client_with(refresh).post(REFRESH_URL, {}, format="json")
        second = self._client_with(refresh).post(REFRESH_URL, {}, format="json")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.cookies["refresh"].value, second.cookies["refresh"].value)
        self.assertEqual(first.cookies["access"].value, second.cookies["access"].value)

        # ...and the rotated token keeps working afterwards
        third = self._client_with(second.cookies["refresh"].value).post(REFRESH_URL, {}, format="json")
        self.assertEqual(third.status_code, 200)

    def test_reused_old_token_rejected_after_grace_expires(self):
        refresh = _login(self.user, APIClient())
        self._client_with(refresh).post(REFRESH_URL, {}, format="json")
        cache.clear()  # grace window over

        res = self._client_with(refresh).post(REFRESH_URL, {}, format="json")
        self.assertIn(res.status_code, (400, 401))

    def test_grace_does_not_revive_logged_out_session(self):
        refresh = _login(self.user, APIClient())
        first = self._client_with(refresh).post(REFRESH_URL, {}, format="json")

        logout_client = self._client_with(first.cookies["refresh"].value)
        logout_client.cookies["access"] = first.cookies["access"].value
        self.assertEqual(logout_client.post("/api/logout/").status_code, 204)

        res = self._client_with(refresh).post(REFRESH_URL, {}, format="json")
        self.assertNotEqual(res.status_code, 200)

    def test_logout_on_one_device_keeps_other_devices_signed_in(self):
        phone = _login(self.user, APIClient())
        laptop = _login(self.user, APIClient())

        phone_client = self._client_with(phone)
        phone_client.cookies["access"] = str(RefreshToken(phone).access_token)
        self.assertEqual(phone_client.post("/api/logout/").status_code, 204)

        res = self._client_with(laptop).post(REFRESH_URL, {}, format="json")
        self.assertEqual(res.status_code, 200)


@override_settings(CACHES=LOCMEM_CACHE)
class VerifyTests(TestCase):
    def test_verify_without_access_cookie_is_401_so_client_refreshes(self):
        res = APIClient().post("/api/jwt/verify/", {}, format="json")
        self.assertEqual(res.status_code, 401)

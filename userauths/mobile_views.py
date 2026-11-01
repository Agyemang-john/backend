"""
userauths/mobile_views.py
Shopper sign-in for the native app.

The web views keep tokens in HTTP-only cookies and strip them from the body.
A native app has no cookie jar it can rely on, so these views speak tokens
only: they come back in the body and go out in the Authorization header.
Cookies are never read or set here, so a page script on our sites can't use
these endpoints to turn the web refresh cookie into readable tokens.

Each access token names its device session in `sid` (the jti of the refresh
token it was minted with, i.e. UserSession.session_key). CustomJWTAuthentication
and the WebSocket middleware already reject revoked sids, so "sign out this
device" and logout cut the app off at once instead of after the access token's
hour runs out.

    POST /api/mobile/auth/login/    {email, password, cf_turnstile_response, device_name?, os?, app_version?}
    POST /api/mobile/auth/refresh/  {refresh}
    POST /api/mobile/auth/logout/   {refresh}
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken, UntypedToken

from address import delivery

from . import captcha
from .custom_throttles import AnonLoginThrottle, LoginThrottle
from .models import UserSession
from .serializers import CustomerCustomTokenRefreshSerializer, CustomTokenObtainPairSerializer
from .session_utils import get_client_ip, register_session, revoke_sessions

User = get_user_model()


def _bind_session(access, refresh):
    """Copy the refresh token's jti onto the access token as `sid`."""
    token = AccessToken(access)
    token["sid"] = UntypedToken(refresh)["jti"]
    return str(token)


def _lifetimes():
    return {
        "access_expires_in": int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds()),
        "refresh_expires_in": int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
    }


def _user_payload(user):
    return {
        "id": user.pk,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "role": user.role,
        "email_verified": user.email_verified_at is not None,
        "phone_verified": user.phone_verified_at is not None,
    }


class MobileLoginSerializer(CustomTokenObtainPairSerializer):
    # Shown on the "where you're signed in" list; user agents from native
    # HTTP stacks (okhttp, CFNetwork) don't name the phone.
    device_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    os = serializers.CharField(max_length=100, required=False, allow_blank=True)
    app_version = serializers.CharField(max_length=40, required=False, allow_blank=True)


class MobileTokenRefreshSerializer(CustomerCustomTokenRefreshSerializer):
    # Body only: the parent falls back to the refresh cookie when this is empty.
    refresh = serializers.CharField(write_only=True)

    def _rotate(self, attrs, refresh_token):
        # Runs before the pair is put in the grace cache, so a concurrent
        # refresh that gets the cached pair gets the bound access token too.
        data = super()._rotate(attrs, refresh_token)
        data["access"] = _bind_session(data["access"], data.get("refresh") or refresh_token)
        return data


class MobileLoginView(APIView):
    """POST /api/mobile/auth/login/"""
    authentication_classes = []  # credentials are in the body; ignore stale tokens
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request):
        if getattr(settings, "MOBILE_AUTH_REQUIRE_CAPTCHA", True):
            token = request.data.get("cf_turnstile_response", "")
            if not captcha.verify_turnstile(token, get_client_ip(request)):
                return Response({"detail": "CAPTCHA verification failed. Please try again."},
                                status=status.HTTP_400_BAD_REQUEST)

        serializer = MobileLoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        tokens = serializer.validated_data
        device = serializer.initial_data

        refresh = UntypedToken(tokens["refresh"])
        user = User.objects.get(pk=refresh["user_id"])

        register_session(user, refresh["jti"], request, is_vendor=False)
        session_fields = {"device_type": "mobile", "browser": "Negromart app"}
        if device.get("device_name"):
            session_fields["device_name"] = device["device_name"]
        if device.get("os"):
            session_fields["os"] = device["os"]
        if device.get("app_version"):
            session_fields["browser"] = f"Negromart app {device['app_version']}"
        UserSession.objects.filter(session_key=refresh["jti"]).update(**session_fields)

        return Response({
            "access": _bind_session(tokens["access"], tokens["refresh"]),
            "refresh": tokens["refresh"],
            **_lifetimes(),
            "user": _user_payload(user),
            # The web keeps this in the ship_country cookie; the app sends it
            # back as X-Country.
            "ship_country": delivery.default_country(user),
        })


class MobileTokenRefreshView(APIView):
    """POST /api/mobile/auth/refresh/"""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_refresh"

    def post(self, request):
        serializer = MobileTokenRefreshSerializer(data=request.data, context={"request": request})
        try:
            serializer.is_valid(raise_exception=True)
        except (serializers.ValidationError, TokenError):
            # One answer for expired, revoked and malformed: the app signs out.
            return Response({"detail": "Session expired. Please log in again.", "code": "session_expired"},
                            status=status.HTTP_401_UNAUTHORIZED)

        data = serializer.validated_data
        return Response({
            "access": data["access"],
            "refresh": data.get("refresh") or request.data["refresh"],
            **_lifetimes(),
        })


class MobileLogoutView(APIView):
    """POST /api/mobile/auth/logout/ — signs out this device only."""
    authentication_classes = []  # holding the refresh token is the proof
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            refresh = RefreshToken(request.data.get("refresh", ""))
        except TokenError:
            # Already expired or revoked: nothing left to sign out.
            return Response(status=status.HTTP_204_NO_CONTENT)

        # Blacklists the refresh token and marks its sid, so the access token
        # in the app stops working now rather than at expiry.
        revoke_sessions(UserSession.objects.filter(session_key=refresh["jti"], is_vendor_session=False))
        refresh.blacklist()  # covers a token with no session row
        return Response(status=status.HTTP_204_NO_CONTENT)

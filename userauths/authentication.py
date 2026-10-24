"""
userauths/authentication.py
Custom JWT authentication backend that supports both customer and vendor tokens.

Token resolution order:
1. Authorization header (standard Bearer token)
2. HTTP-only cookie (customer: settings.AUTH_COOKIE, vendor: settings.VENDOR_ACCESS_AUTH_COOKIE)

The X-User-Type header ("customer" or "vendor") determines which cookie to read.
Vendor routes (except /vendor/register/) also enforce user.is_vendor: an active
membership in an approved store (vendor/access.py).

token_version check: if the JWT payload carries a token_version claim that no longer
matches the user's DB value, the token is treated as unauthenticated.  Incrementing
user.token_version (e.g. on "logout all devices") immediately invalidates every
existing access token without a blacklist table.

sid check: seller tokens name their device session; signing that device out
(session_utils.revoke_sessions) rejects its access token on the next request.
"""

from urllib.parse import urlsplit

from django.conf import settings
from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication

from .session_utils import is_session_revoked
from .tokens import is_seller_token

# Set on request.user: 'seller' or 'shopper', by which sign-in issued the token.
AUTH_SCOPE_ATTR = '_auth_scope'


_SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS', 'TRACE')


def _trusted_origins():
    return {
        o.rstrip('/').lower()
        for o in list(getattr(settings, 'CORS_ALLOWED_ORIGINS', [])) + list(getattr(settings, 'CSRF_TRUSTED_ORIGINS', []))
    }


def check_cookie_request_origin(request):
    """
    CSRF guard for cookie-authenticated writes. Browsers attach Origin (or at
    least Referer) to every cross-site POST, so a write whose Origin is not
    one of our sites was started by some other website. Requests with neither
    header come from servers (our SSR), not from a victim's browser.
    """
    if request.method in _SAFE_METHODS:
        return
    origin = request.headers.get('Origin')
    if not origin or origin == 'null':
        referer = request.headers.get('Referer')
        if referer:
            parts = urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}"
        elif origin == 'null':
            raise exceptions.PermissionDenied("Cross-site request blocked.")
        else:
            return
    allowed = _trusted_origins()
    host = request.get_host()
    allowed.add(f"{'https' if request.is_secure() else 'http'}://{host}".lower())
    if origin.rstrip('/').lower() not in allowed:
        raise exceptions.PermissionDenied("Cross-site request blocked.")


class CustomJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        raw_token = None
        auth_header = self.get_header(request)

        if auth_header:
            raw_token = self.get_raw_token(auth_header)
        header_token = raw_token

        expected_type = request.headers.get("X-User-Type", "customer").lower()

        if expected_type == "vendor":
            if request.path in ['/api/v1/vendor/register/', '/api/v1/vendor/register']:
                raw_token = raw_token or request.COOKIES.get(settings.AUTH_COOKIE)
            else:
                raw_token = request.COOKIES.get(
                    getattr(settings, "VENDOR_ACCESS_AUTH_COOKIE", "vendor_access")
                )

            if not raw_token:
                return None

        else:
            raw_token = raw_token or request.COOKIES.get(settings.AUTH_COOKIE)

        if not raw_token:
            return None

        # A token in the Authorization header can't be sent by another site;
        # a cookie can, so cookie-authenticated writes must come from our sites.
        if header_token is None or raw_token != header_token:
            check_cookie_request_origin(request)

        try:
            validated_token = self.get_validated_token(raw_token)
            user = self.get_user(validated_token)

            # token_version: catches "logout all devices" immediately
            token_version = validated_token.get('token_version')
            if token_version is not None and token_version != user.token_version:
                return None  # treat as unauthenticated

            # sid: catches "sign out this device" before the access token expires
            sid = validated_token.get('sid')
            if sid and is_session_revoked(sid):
                return None

            # Seller access needs a token from the seller sign-in (password +
            # code). A shopper token in the vendor cookie is refused, and a
            # shopper token anywhere else authenticates the person but not
            # their store: vendor/access.py denies dashboard access for it.
            seller_token = is_seller_token(validated_token)
            if expected_type == "vendor" and '/vendor/register' not in request.path and not seller_token:
                return None
            user.__dict__[AUTH_SCOPE_ATTR] = 'seller' if seller_token else 'shopper'

            # Vendor cookie ⇒ the user must still belong to an approved store.
            # Checked on every request (one memoised query), so removing a team
            # member or suspending a store cuts dashboard access immediately,
            # without waiting for the access token to expire.
            if expected_type == "vendor" and '/vendor/register' not in request.path:
                if not user.is_vendor:
                    return None

            return (user, validated_token)
        except Exception:
            return None

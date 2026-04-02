"""
userauths/authentication.py
Custom JWT authentication backend that supports both customer and vendor tokens.

Token resolution order:
1. Authorization header (standard Bearer token)
2. HTTP-only cookie (customer: settings.AUTH_COOKIE, vendor: settings.VENDOR_ACCESS_AUTH_COOKIE)

The X-User-Type header ("customer" or "vendor") determines which cookie to read.
Vendor routes (except /vendor/register/) also enforce that the user has role='vendor'.
"""

from rest_framework_simplejwt.authentication import JWTAuthentication
from django.conf import settings

class CustomJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        raw_token = None
        auth_header = self.get_header(request)

        if auth_header:
            raw_token = self.get_raw_token(auth_header)

        expected_type = request.headers.get("X-User-Type", "customer").lower()

        if expected_type == "vendor":
            # SPECIAL CASE: Allow customer token ONLY on the exact registration endpoint
            if request.path in ['/api/v1/vendor/register/', '/api/v1/vendor/register']:
                raw_token = raw_token or request.COOKIES.get(settings.AUTH_COOKIE)
            else:
                raw_token = request.COOKIES.get(getattr(settings, "VENDOR_ACCESS_AUTH_COOKIE", "vendor_access"))

            if not raw_token:
                return None  # 401

        else:
            raw_token = raw_token or request.COOKIES.get(settings.AUTH_COOKIE)

        if not raw_token:
            return None

        try:
            validated_token = self.get_validated_token(raw_token)
            user = self.get_user(validated_token)

            # Final safety: vendor routes require vendor role (except during registration)
            if expected_type == "vendor" and '/vendor/register' not in request.path:
                if getattr(user, 'role', None) != 'vendor':
                    return None

            return (user, validated_token)
        except Exception:
            return None
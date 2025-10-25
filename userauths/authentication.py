from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication

class CustomJWTAuthentication(JWTAuthentication):
    """
    Custom authentication class that supports:
      - Normal users via `settings.AUTH_COOKIE`
      - Sellers via `settings.VENDOR_AUTH_COOKIE`
      - Fallback to Authorization header (Bearer token)
    """

    def authenticate(self, request):
        try:
            header = self.get_header(request)
            raw_token = None

            if header is not None:
                raw_token = self.get_raw_token(header)

            if raw_token is None:
                raw_token = request.COOKIES.get(settings.AUTH_COOKIE)

            if raw_token is None:
                raw_token = request.COOKIES.get(
                    getattr(settings, "VENDOR_AUTH_COOKIE", "vendor_access")
                )

            if raw_token is None:
                return None

            validated_token = self.get_validated_token(raw_token)

            user = self.get_user(validated_token)

            return (user, validated_token)
        except Exception:
            return None

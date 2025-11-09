from rest_framework_simplejwt.authentication import JWTAuthentication
from django.conf import settings

class CustomJWTAuthentication(JWTAuthentication):
    """
    Custom JWT Authentication that supports both customer and vendor tokens.
    Chooses which token to use based on the `X-User-Type` request header.
    """

    def authenticate(self, request):
        try:
            user_type = request.headers.get("X-User-Type", "customer").lower()
            raw_token = None

            # Check Authorization header first (standard)
            header = self.get_header(request)
            if header is not None:
                raw_token = self.get_raw_token(header)

            # If no Authorization header, fall back to cookies
            if raw_token is None:
                if user_type == "vendor":
                    raw_token = request.COOKIES.get(
                        getattr(settings, "VENDOR_ACCESS_AUTH_COOKIE", "vendor_access")
                    )
                else:
                    raw_token = request.COOKIES.get(
                        getattr(settings, "AUTH_COOKIE", "access")
                    )

            if raw_token is None:
                return None

            validated_token = self.get_validated_token(raw_token)
            user = self.get_user(validated_token)

            return (user, validated_token)

        except Exception:
            return None

# userauths/authentication.py
from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication

class CustomJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        try:
            # First try to get token from header
            header = self.get_header(request)
            
            if header is None:
                # If no header, try to get from cookie
                raw_token = request.COOKIES.get(settings.AUTH_COOKIE)
            else:
                raw_token = self.get_raw_token(header)
            
            if raw_token is None:
                return None
                
            validated_token = self.get_validated_token(raw_token)
            return self.get_user(validated_token), validated_token
        except Exception:
            return None
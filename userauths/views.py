
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from djoser.social.views import ProviderAuthView
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView
)
from .serializers import CustomTokenObtainPairSerializer
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth import get_user_model
from .custom_throttles import LoginThrottle, AnonLoginThrottle, CheckoutThrottle, PasswordResetThrottle
User = get_user_model()
from rest_framework.permissions import AllowAny

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]  # allow guests to login
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)

        if response.status_code == 200:
            access_token = response.data.get("access")
            refresh_token = response.data.get("refresh")

            # Set HTTP-only cookies
            response.set_cookie(
                "access",
                access_token,
                max_age=settings.AUTH_ACCESS_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN
            )
            response.set_cookie(
                "refresh",
                refresh_token,
                max_age=settings.AUTH_REFRESH_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN
            )

            # Remove tokens from response body for extra security
            del response.data["access"]
            del response.data["refresh"]

        return response

class CustomTokenRefreshView(TokenRefreshView):
    permission_classes = [AllowAny]
    throttle_scope = "auth_refresh"

    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get('refresh')

        if refresh_token:
            request.data['refresh'] = refresh_token

        response = super().post(request, *args, **kwargs)

        if response.status_code == 200:
            access_token = response.data.get('access')

            # Set the token in cookie
            response.set_cookie(
                "access",
                access_token,
                max_age=settings.AUTH_ACCESS_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN
            )

            # ✅ Keep it in the response body *only* if from SSR (optional header flag)
            if request.headers.get("X-SSR-Refresh") == "true":
                pass  # leave token in body for SSR
            else:
                del response.data["access"]

        return response



class CustomTokenVerifyView(TokenVerifyView):
    permission_classes = [AllowAny]
    throttle_scope = "auth_verify"
    
    def post(self, request, *args, **kwargs):
        access_token = request.COOKIES.get('access')

        if access_token:
            request.data['token'] = access_token

        return super().post(request, *args, **kwargs)



class LogoutView(APIView):
    def post(self, request, *args, **kwargs):
        response = Response(status=status.HTTP_204_NO_CONTENT)
        response.delete_cookie('access')
        response.delete_cookie('refresh')

        return response

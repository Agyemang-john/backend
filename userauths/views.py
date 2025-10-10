
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
from .serializers import CustomTokenObtainPairSerializer, CustomTokenRefreshSerializer, otp_token_generator
from django.contrib.auth import get_user_model
from .custom_throttles import LoginThrottle, AnonLoginThrottle, CheckoutThrottle, PasswordResetThrottle
from rest_framework.permissions import AllowAny
from .tasks import send_otp
from .vendor_serializers import VendorLoginSerializer
from django.core.cache import cache
from rest_framework_simplejwt.tokens import RefreshToken
from django.conf import settings
from django.db.models import Q
from django.core.mail import send_mail
User = get_user_model()
import time
from vendor.models import Vendor

# customers login
class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]  # allow guests to login
    # throttle_classes = [LoginThrottle, AnonLoginThrottle]

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
    serializer_class = CustomTokenRefreshSerializer
    permission_classes = [AllowAny]
    # throttle_scope = "auth_refresh"

    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get("refresh")
        if refresh_token:
            request.data["refresh"] = refresh_token

        response = super().post(request, *args, **kwargs)

        if response.status_code == 200:
            access_token = response.data.get("access")

            response.set_cookie(
                "access",
                access_token,
                max_age=settings.AUTH_ACCESS_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN,
            )

            if request.headers.get("X-SSR-Refresh") != "true":
                del response.data["access"]

        return response
    
# sellers login
class CustomVendorTokenRefreshView(TokenRefreshView):
    serializer_class = CustomTokenRefreshSerializer
    permission_classes = [AllowAny]
    throttle_scope = "auth_refresh"

    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get("refresh")
        if refresh_token:
            request.data["refresh"] = refresh_token

        response = super().post(request, *args, **kwargs)

        if response.status_code == 200:
            access_token = response.data.get("access")

            response.set_cookie(
                "access",
                access_token,
                max_age=settings.VENDOR_AUTH_ACCESS_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN,
            )

            if request.headers.get("X-SSR-Refresh") != "true":
                del response.data["access"]

        return response

class CustomVendorTokenObtainPairView(TokenObtainPairView):
    serializer_class = VendorLoginSerializer
    permission_classes = [AllowAny]  # allow guests to login
    # throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)

        if response.status_code == 200 and 'access' in response.data:
            access_token = response.data.get("access")
            refresh_token = response.data.get("refresh")

            response.set_cookie(
                "access",
                access_token,
                max_age=settings.VENDOR_AUTH_ACCESS_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN
            )
            response.set_cookie(
                "refresh",
                refresh_token,
                max_age=settings.VENDOR_AUTH_REFRESH_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN
            )

            del response.data["access"]
            del response.data["refresh"]

        return response

class OTPVerifyView(APIView):
    permission_classes = [AllowAny]
    # throttle_classes = [LoginThrottle]

    def post(self, request, *args, **kwargs):
        email_or_phone = request.data.get("email")
        otp = request.data.get("otp")

        try:
            user = User.objects.get(Q(email__iexact=email_or_phone) | Q(phone=email_or_phone))
            if user.role != 'vendor':
                return Response({'detail': 'OTP verification not required for this user.'}, status=status.HTTP_400_BAD_REQUEST)

            cached_data = cache.get(f"otp_{user.id}")
            if cached_data and 'otp' in cached_data and 'timestamp' in cached_data:
                if str(cached_data['otp']) == otp and not otp_token_generator._is_token_expired(cached_data['timestamp']):
                    cache.delete(f"otp_{user.id}")
                    refresh = RefreshToken.for_user(user)
                    refresh["role"] = user.role
                    refresh["is_admin"] = user.is_admin
                    refresh["is_active"] = user.is_active
                    # Add is_verified_vendor flag
                    refresh["is_verified_vendor"] = False
                    if user.role == 'vendor':
                        try:
                            vendor = user.vendor_user
                            refresh["is_verified_vendor"] = (
                                vendor.status == 'VERIFIED' and
                                vendor.is_approved and
                                not vendor.is_suspended
                                # Optionally: and vendor.has_active_subscription()
                            )
                        except Vendor.DoesNotExist:
                            pass  # Leave as False

                    access_token = str(refresh.access_token)
                    refresh_token = str(refresh)

                    response = Response({'detail': 'Login successful.'}, status=status.HTTP_200_OK)
                    response.set_cookie(
                        "access",
                        access_token,
                        max_age=settings.VENDOR_AUTH_ACCESS_MAX_AGE,
                        path=settings.AUTH_COOKIE_PATH,
                        secure=settings.AUTH_COOKIE_SECURE,
                        httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                        samesite=settings.AUTH_COOKIE_SAMESITE,
                        domain=settings.AUTH_COOKIE_DOMAIN
                    )
                    response.set_cookie(
                        "refresh",
                        refresh_token,
                        max_age=settings.VENDOR_AUTH_REFRESH_MAX_AGE,
                        path=settings.AUTH_COOKIE_PATH,
                        secure=settings.AUTH_COOKIE_SECURE,
                        httponly=settings.AUTH_COOKIE_HTTP_ONLY,
                        samesite=settings.AUTH_COOKIE_SAMESITE,
                        domain=settings.AUTH_COOKIE_DOMAIN
                    )
                    return response
                else:
                    return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_400_BAD_REQUEST)
        
class OTPResendView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request, *args, **kwargs):
        email_or_phone = request.data.get('email')
        if not email_or_phone:
            return Response({'detail': 'Email or phone required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user = User.objects.get(Q(email__iexact=email_or_phone) | Q(phone=email_or_phone))
            if user.role != 'vendor':
                return Response({'detail': 'OTP verification not required for this user.'}, status=status.HTTP_400_BAD_REQUEST)
            otp = otp_token_generator.make_token(user)
            cache.set(f"otp_{user.id}", {'otp': otp, 'timestamp': time.time()}, timeout=600)
            # Send OTP via Celery task (Arkesel for SMS, Django for email)
            recipient = user.email if '@' in email_or_phone else user.phone
            is_email = '@' in email_or_phone
            send_otp.delay(recipient, otp, is_email)
            return Response({'detail': 'OTP sent to your email or phone.'}, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_400_BAD_REQUEST)

# Token verification for both
class CustomTokenVerifyView(TokenVerifyView):
    permission_classes = [AllowAny]
    throttle_scope = "auth_verify"
    
    def post(self, request, *args, **kwargs):
        access_token = request.COOKIES.get('access')

        if access_token:
            request.data['token'] = access_token

        return super().post(request, *args, **kwargs)


# logout for both
class LogoutView(APIView):
    def post(self, request, *args, **kwargs):
        response = Response(status=status.HTTP_204_NO_CONTENT)
        response.delete_cookie('access')
        response.delete_cookie('refresh')

        return response

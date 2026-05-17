
import hmac
import logging

from django.conf import settings
from userauths.tokens import CustomVendorRefreshToken
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView
)
from .serializers import CustomTokenObtainPairSerializer, otp_token_generator
from django.contrib.auth import get_user_model
from .custom_throttles import LoginThrottle, AnonLoginThrottle
from rest_framework.permissions import AllowAny
from .tasks import send_otp
from .vendor_serializers import VendorLoginSerializer
from django.core.cache import cache
from django.db.models import Q
User = get_user_model()
import time
from rest_framework import generics
from userauths.serializers import RegisterSerializer
from django.utils.http import urlsafe_base64_decode
from django.contrib.auth.tokens import default_token_generator
from django.utils import timezone

logger = logging.getLogger(__name__)

# Max OTP attempts before the cached OTP is invalidated (per 5-minute window)
OTP_MAX_ATTEMPTS = 5
OTP_ATTEMPT_TTL  = 300  # seconds

class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]


class ActivateEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        uidb64 = request.data.get('uid')
        token = request.data.get('token')
        try:
            uid = urlsafe_base64_decode(uidb64).decode()
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({
                "success": False,
                "message": "Activation link is invalid or expired."
            }, status=status.HTTP_400_BAD_REQUEST)

        if user.is_active:
            return Response({
                "success": False,
                "message": "Account is already activated."
            }, status=status.HTTP_400_BAD_REQUEST)

        if not default_token_generator.check_token(user, token):
            return Response({
                "success": False,
                "message": "Activation link is invalid or expired."
            }, status=status.HTTP_400_BAD_REQUEST)

        user.is_active = True
        user.save()
        return Response({
            "success": True,
            "message": "Email verified successfully. You can now log in."
        }, status=status.HTTP_200_OK)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)

        ip = request.META.get("REMOTE_ADDR")
        if response.status_code == 200:
            logger.info("customer.login_success ip=%s", ip)
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

from .serializers import CustomerCustomTokenRefreshSerializer
class CustomTokenRefreshView(TokenRefreshView):
    serializer_class = CustomerCustomTokenRefreshSerializer
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)

        if response.status_code == 200:
            access_token = response.data.get("access")

            # Set new access cookie
            response.set_cookie(
                "access", access_token,
                max_age=settings.AUTH_ACCESS_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=True,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN,
            )
            if request.headers.get("X-SSR-Refresh") != "true":
                response.data = {}

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
        
        # Delete cookies using only supported args
        response.delete_cookie(
            'access',
            path=settings.AUTH_COOKIE_PATH,
            domain=settings.AUTH_COOKIE_DOMAIN,
        )
        response.delete_cookie(
            'refresh',
            path=settings.AUTH_COOKIE_PATH,
            domain=settings.AUTH_COOKIE_DOMAIN,
        )
        return response
    

# Vendor auth 
####################################
from userauths.vendor_serializers import CustomTokenRefreshSerializer
class VendorTokenRefreshView(TokenRefreshView):
    serializer_class = CustomTokenRefreshSerializer
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get("vendor_refresh")
        if refresh_token:
            request.data["refresh"] = refresh_token  # mutable

        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            access_token = response.data.get("access")
            response.set_cookie(
                settings.VENDOR_ACCESS_AUTH_COOKIE,
                access_token,
                max_age=settings.VENDOR_AUTH_ACCESS_MAX_AGE,
                path=settings.VENDOR_AUTH_COOKIE_PATH,
                secure=settings.VENDOR_AUTH_COOKIE_SECURE,
                httponly=settings.VENDOR_AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
                domain=settings.VENDOR_AUTH_COOKIE_DOMAIN
            )
            if request.headers.get("X-SSR-Refresh") != "true":
                del response.data["access"]
        return response

class VendorTokenObtainPairView(TokenObtainPairView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request):
        serializer = VendorLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        logger.info("vendor.otp_sent ip=%s", request.META.get("REMOTE_ADDR"))
        return Response({"detail": "OTP sent. Please verify to continue."}, status=200)

class VendorOTPVerifyView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AnonLoginThrottle]

    def post(self, request, *args, **kwargs):
        email_or_phone = request.data.get("email")
        otp = request.data.get("otp", "")
        ip = request.META.get("REMOTE_ADDR")

        try:
            user = User.objects.get(Q(email__iexact=email_or_phone) | Q(phone=email_or_phone))
        except User.DoesNotExist:
            logger.warning("vendor.otp_verify unknown_identifier ip=%s", ip)
            return Response({'detail': 'Invalid credentials.'}, status=status.HTTP_400_BAD_REQUEST)

        if user.role != 'vendor':
            return Response({'detail': 'Invalid credentials.'}, status=status.HTTP_400_BAD_REQUEST)

        # Per-user attempt counter — prevents OTP brute-force regardless of IP throttle
        attempts_key = f"otp:attempts:{user.id}"
        attempts = cache.get(attempts_key, 0)
        if attempts >= OTP_MAX_ATTEMPTS:
            logger.warning("vendor.otp_verify locked user=%s ip=%s", user.id, ip)
            cache.delete(f"otp_{user.id}")  # invalidate the OTP so a new one must be requested
            return Response(
                {'detail': 'Too many invalid attempts. Please request a new OTP.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        cached_data = cache.get(f"otp_{user.id}")
        if not (cached_data and 'otp' in cached_data and 'timestamp' in cached_data):
            return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        # Constant-time comparison prevents timing attacks
        otp_match = hmac.compare_digest(str(cached_data['otp']), str(otp))
        if otp_match and not otp_token_generator._is_token_expired(cached_data['timestamp']):
            cache.delete(f"otp_{user.id}")
            cache.delete(attempts_key)
            logger.info("vendor.login_success user=%s ip=%s", user.id, ip)

            refresh = CustomVendorRefreshToken.for_user(user)
            access_token = str(refresh.access_token)
            refresh_token = str(refresh)

            response = Response({'detail': 'Login successful.'}, status=status.HTTP_200_OK)
            response.set_cookie(
                settings.VENDOR_ACCESS_AUTH_COOKIE,
                access_token,
                max_age=settings.VENDOR_AUTH_ACCESS_MAX_AGE,
                path=settings.VENDOR_AUTH_COOKIE_PATH,
                secure=settings.VENDOR_AUTH_COOKIE_SECURE,
                httponly=settings.VENDOR_AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
                domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
            )
            response.set_cookie(
                settings.VENDOR_REFRESH_AUTH_COOKIE,
                refresh_token,
                max_age=settings.VENDOR_AUTH_REFRESH_MAX_AGE,
                path=settings.VENDOR_AUTH_COOKIE_PATH,
                secure=settings.VENDOR_AUTH_COOKIE_SECURE,
                httponly=settings.VENDOR_AUTH_COOKIE_HTTP_ONLY,
                samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
                domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
            )
            return response

        # Failed attempt — increment counter
        cache.set(attempts_key, attempts + 1, timeout=OTP_ATTEMPT_TTL)
        logger.warning("vendor.otp_verify failed user=%s attempt=%d ip=%s", user.id, attempts + 1, ip)
        return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

class VendorOTPResendView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request, *args, **kwargs):
        email_or_phone = request.data.get('email')
        if not email_or_phone:
            return Response({'detail': 'Email or phone required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Always return the same response to prevent account enumeration
        generic_ok = Response({'detail': 'If an account exists, an OTP has been sent.'}, status=status.HTTP_200_OK)

        try:
            user = User.objects.get(Q(email__iexact=email_or_phone) | Q(phone=email_or_phone))
        except User.DoesNotExist:
            logger.info("vendor.otp_resend unknown_identifier ip=%s", request.META.get("REMOTE_ADDR"))
            return generic_ok  # same response — no enumeration

        if user.role != 'vendor':
            return generic_ok

        # Reset attempt counter so vendor can try again with the new OTP
        cache.delete(f"otp:attempts:{user.id}")

        otp = otp_token_generator.make_token(user)
        cache.set(f"otp_{user.id}", {'otp': otp, 'timestamp': time.time()}, timeout=600)
        recipient = user.email if '@' in email_or_phone else user.phone
        is_email = '@' in email_or_phone
        send_otp.delay(recipient, otp, is_email)
        logger.info("vendor.otp_resend user=%s ip=%s", user.id, request.META.get("REMOTE_ADDR"))
        return generic_ok

class VendorTokenVerifyView(TokenVerifyView):
    permission_classes = [AllowAny]
    # throttle_scope = "auth_verify"
    
    def post(self, request, *args, **kwargs):
        access_token = request.COOKIES.get('vendor_access')

        if access_token:
            request.data['token'] = access_token

        return super().post(request, *args, **kwargs)

class VendorLogoutView(APIView):
    def post(self, request, *args, **kwargs):
        response = Response(status=status.HTTP_204_NO_CONTENT)
        
        # Delete cookies using only supported args
        response.delete_cookie(
            settings.VENDOR_ACCESS_AUTH_COOKIE,
            path=settings.VENDOR_AUTH_COOKIE_PATH,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
        )
        response.delete_cookie(
            settings.VENDOR_REFRESH_AUTH_COOKIE,
            path=settings.VENDOR_AUTH_COOKIE_PATH,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN
        )
        return response


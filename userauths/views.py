
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
from .custom_throttles import LoginThrottle
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
    permission_classes = [AllowAny]  # allow guests to login
    # throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request):
        serializer = VendorLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"detail": "OTP sent. Please verify to continue."}, status=200)

class VendorOTPVerifyView(APIView):
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
                    refresh = CustomVendorRefreshToken.for_user(user)
                    access_token = str(refresh.access_token)
                    refresh_token = str(refresh)

                    response = Response({'detail': 'Login successful.'}, status=status.HTTP_200_OK)
                    # if response.status_code == 200 and 'vendor_access' in response.data:
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
                    response.set_cookie(
                        settings.VENDOR_REFRESH_AUTH_COOKIE,
                        refresh_token,
                        max_age=settings.VENDOR_AUTH_REFRESH_MAX_AGE,
                        path=settings.VENDOR_AUTH_COOKIE_PATH,
                        secure=settings.VENDOR_AUTH_COOKIE_SECURE,
                        httponly=settings.VENDOR_AUTH_COOKIE_HTTP_ONLY,
                        samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
                        domain=settings.VENDOR_AUTH_COOKIE_DOMAIN
                    )
                    return response
                else:
                    return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_400_BAD_REQUEST)

class VendorOTPResendView(APIView):
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


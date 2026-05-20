
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
from rest_framework.permissions import AllowAny, IsAuthenticated
from .tasks import send_otp
from .vendor_serializers import VendorLoginSerializer
from django.core.cache import cache
from django.db.models import Q, F
User = get_user_model()
import time
from rest_framework import generics
from userauths.serializers import RegisterSerializer
from django.utils.http import urlsafe_base64_decode
from django.contrib.auth.tokens import default_token_generator
from django.utils import timezone
from .session_utils import register_session, get_client_ip

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

            # Register device session
            if refresh_token:
                try:
                    from rest_framework_simplejwt.tokens import RefreshToken as _RT
                    rt = _RT(refresh_token)
                    jti = rt.payload.get('jti')
                    user_id = rt.payload.get('user_id')
                    if jti and user_id:
                        user = User.objects.get(id=user_id)
                        register_session(user, jti, request, is_vendor=False)
                except Exception:
                    pass

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
    throttle_classes = [LoginThrottle]

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

                    # Register device session (jti is inside the refresh token payload)
                    try:
                        jti = refresh.payload.get('jti')
                        if jti:
                            register_session(user, jti, request, is_vendor=True)
                    except Exception:
                        pass

                    response = Response({'detail': 'Login successful.'}, status=status.HTTP_200_OK)

                    # Fire async activity tracking (login event + last_seen update)
                    try:
                        from vendor.tasks import log_vendor_activity
                        from vendor.models import Vendor
                        from django.core.cache import cache as dj_cache
                        from django_redis import get_redis_connection
                        vendor = Vendor.objects.filter(user=user).first()
                        if vendor:
                            ip = (
                                request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                                or request.META.get('REMOTE_ADDR')
                            )
                            ua = request.META.get('HTTP_USER_AGENT', '')[:500]
                            was_auto_closed = vendor.inactivity_auto_closed
                            log_vendor_activity.delay(vendor.pk, 'login', ip, ua)
                            Vendor.objects.filter(pk=vendor.pk).update(
                                last_login_at=timezone.now(),
                                last_seen_at=timezone.now(),
                                total_login_count=vendor.total_login_count + 1,
                                inactivity_auto_closed=False,
                            )
                            # Warm the middleware uid→vid cache and seed last_seen in Redis
                            try:
                                conn = get_redis_connection("default")
                                conn.set(f"vendor:uid_vid:{user.id}", vendor.pk, ex=86400)
                                conn.set(f"vendor:last_seen:{vendor.pk}", int(time.time()), ex=86400)
                            except Exception:
                                pass
                            if was_auto_closed:
                                log_vendor_activity.delay(vendor.pk, 'auto_reopen', ip, ua)
                    except Exception:
                        pass

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
            otp = otp_token_generator.generate_otp()
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
        # Fire async logout activity before clearing auth
        if request.user.is_authenticated and getattr(request.user, 'role', None) == 'vendor':
            try:
                from vendor.tasks import log_vendor_activity
                from vendor.models import Vendor
                vendor = Vendor.objects.filter(user=request.user).first()
                if vendor:
                    ip = (
                        request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                        or request.META.get('REMOTE_ADDR')
                    )
                    ua = request.META.get('HTTP_USER_AGENT', '')[:500]
                    log_vendor_activity.delay(vendor.pk, 'logout', ip, ua)
                    Vendor.objects.filter(pk=vendor.pk).update(last_logout_at=timezone.now())
            except Exception:
                pass

        # Delete current device session
        refresh_cookie = request.COOKIES.get(settings.VENDOR_REFRESH_AUTH_COOKIE)
        if refresh_cookie:
            try:
                from rest_framework_simplejwt.tokens import RefreshToken as _RT
                rt = _RT(refresh_cookie)
                jti = rt.payload.get('jti')
                if jti:
                    from .models import UserSession
                    UserSession.objects.filter(session_key=jti).delete()
            except Exception:
                pass

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


# ── Session management views ──────────────────────────────────────────────────

def _get_current_jti(request, cookie_name: str):
    """Return the jti of the refresh token in the given cookie, or None."""
    token_str = request.COOKIES.get(cookie_name)
    if not token_str:
        return None
    try:
        from rest_framework_simplejwt.tokens import RefreshToken as _RT
        return _RT(token_str).payload.get('jti')
    except Exception:
        return None


def _session_to_dict(session, current_jti) -> dict:
    return {
        'id': str(session.id),
        'device_type': session.device_type,
        'device_name': session.device_name,
        'browser': session.browser,
        'os': session.os,
        'ip_address': session.ip_address,
        'last_activity': session.last_activity,
        'created_at': session.created_at,
        'is_current': session.session_key == current_jti,
    }


class VendorSessionListView(APIView):
    """GET /api/v1/auth/vendor/sessions/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import UserSession
        current_jti = _get_current_jti(request, settings.VENDOR_REFRESH_AUTH_COOKIE)
        sessions = UserSession.objects.filter(
            user=request.user, is_vendor_session=True
        ).order_by('-last_activity')
        return Response([_session_to_dict(s, current_jti) for s in sessions])


class VendorSessionRevokeView(APIView):
    """DELETE /api/v1/auth/vendor/sessions/<uuid>/"""
    permission_classes = [IsAuthenticated]

    def delete(self, request, session_id):
        from .models import UserSession
        current_jti = _get_current_jti(request, settings.VENDOR_REFRESH_AUTH_COOKIE)
        try:
            session = UserSession.objects.get(
                id=session_id, user=request.user, is_vendor_session=True
            )
        except UserSession.DoesNotExist:
            return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

        if current_jti and session.session_key == current_jti:
            return Response(
                {'detail': 'Cannot revoke your current session. Use "Log out" instead.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class VendorLogoutAllView(APIView):
    """POST /api/v1/auth/vendor/logout-all/"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .models import UserSession
        # Incrementing token_version immediately invalidates every existing JWT
        User.objects.filter(pk=request.user.pk).update(
            token_version=F('token_version') + 1
        )
        UserSession.objects.filter(user=request.user, is_vendor_session=True).delete()

        response = Response({'detail': 'Logged out from all devices.'}, status=status.HTTP_200_OK)
        response.delete_cookie(
            settings.VENDOR_ACCESS_AUTH_COOKIE,
            path=settings.VENDOR_AUTH_COOKIE_PATH,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
        )
        response.delete_cookie(
            settings.VENDOR_REFRESH_AUTH_COOKIE,
            path=settings.VENDOR_AUTH_COOKIE_PATH,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
        )
        return response


class VendorLogoutOtherSessionsView(APIView):
    """POST /api/v1/auth/vendor/logout-other-sessions/"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .models import UserSession
        current_jti = _get_current_jti(request, settings.VENDOR_REFRESH_AUTH_COOKIE)
        qs = UserSession.objects.filter(user=request.user, is_vendor_session=True)
        if current_jti:
            qs = qs.exclude(session_key=current_jti)
        count = qs.count()
        qs.delete()
        return Response(
            {'detail': f'Logged out from {count} other device(s).'},
            status=status.HTTP_200_OK,
        )


# Customer session management (mirrors vendor pattern for buyer accounts)

class CustomerSessionListView(APIView):
    """GET /api/v1/auth/sessions/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import UserSession
        current_jti = _get_current_jti(request, 'refresh')
        sessions = UserSession.objects.filter(
            user=request.user, is_vendor_session=False
        ).order_by('-last_activity')
        return Response([_session_to_dict(s, current_jti) for s in sessions])


class CustomerSessionRevokeView(APIView):
    """DELETE /api/v1/auth/sessions/<uuid>/"""
    permission_classes = [IsAuthenticated]

    def delete(self, request, session_id):
        from .models import UserSession
        current_jti = _get_current_jti(request, 'refresh')
        try:
            session = UserSession.objects.get(
                id=session_id, user=request.user, is_vendor_session=False
            )
        except UserSession.DoesNotExist:
            return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

        if current_jti and session.session_key == current_jti:
            return Response(
                {'detail': 'Cannot revoke your current session. Use "Log out" instead.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomerLogoutAllView(APIView):
    """POST /api/v1/auth/logout-all/"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .models import UserSession
        User.objects.filter(pk=request.user.pk).update(
            token_version=F('token_version') + 1
        )
        UserSession.objects.filter(user=request.user, is_vendor_session=False).delete()

        response = Response({'detail': 'Logged out from all devices.'}, status=status.HTTP_200_OK)
        response.delete_cookie('access', path=settings.AUTH_COOKIE_PATH, domain=settings.AUTH_COOKIE_DOMAIN)
        response.delete_cookie('refresh', path=settings.AUTH_COOKIE_PATH, domain=settings.AUTH_COOKIE_DOMAIN)
        return response

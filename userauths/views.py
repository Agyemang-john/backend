
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
from .custom_throttles import LoginThrottle, AnonLoginThrottle, RegisterThrottle
from rest_framework.permissions import AllowAny, IsAuthenticated
from .tasks import send_otp
from .vendor_serializers import VendorLoginSerializer
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
    throttle_classes = [RegisterThrottle]

    def post(self, request, *args, **kwargs):
        from .captcha import verify_turnstile
        token = request.data.get('cf_turnstile_response', '')
        ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')
        if not verify_turnstile(token, ip):
            return Response({'detail': 'CAPTCHA verification failed. Please try again.'}, status=status.HTTP_400_BAD_REQUEST)
        return super().post(request, *args, **kwargs)


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
        from .captcha import verify_turnstile
        token = request.data.get('cf_turnstile_response', '')
        ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')
        if not verify_turnstile(token, ip):
            return Response({'detail': 'CAPTCHA verification failed. Please try again.'}, status=status.HTTP_400_BAD_REQUEST)
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
        refresh_cookie = request.COOKIES.get('refresh')
        if refresh_cookie:
            try:
                from rest_framework_simplejwt.tokens import RefreshToken as _RT
                from .models import UserSession
                rt = _RT(refresh_cookie)
                jti = rt.payload.get('jti')
                if jti:
                    UserSession.objects.filter(session_key=jti).delete()
                # Blacklist the refresh token so it cannot be reused
                rt.blacklist()
            except Exception:
                pass

        # Invalidate all sessions for this user by bumping token_version
        if request.user.is_authenticated:
            User.objects.filter(pk=request.user.pk).update(
                token_version=F('token_version') + 1
            )

        response = Response(status=status.HTTP_204_NO_CONTENT)
        response.delete_cookie('access', path=settings.AUTH_COOKIE_PATH, domain=settings.AUTH_COOKIE_DOMAIN)
        response.delete_cookie('refresh', path=settings.AUTH_COOKIE_PATH, domain=settings.AUTH_COOKIE_DOMAIN)
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

def _mask_identifier(value: str) -> str:
    """Return a redacted version safe to display in the UI (e.g. j***@gmail.com)."""
    if '@' in value:
        local, domain = value.split('@', 1)
        return local[:1] + '***@' + domain
    # phone: show last 3 digits
    return '***' + value[-3:]


class VendorTokenObtainPairView(TokenObtainPairView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request):
        from .captcha import verify_turnstile
        token = request.data.get('cf_turnstile_response', '')
        ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')
        if not verify_turnstile(token, ip):
            return Response({'detail': 'CAPTCHA verification failed. Please try again.'}, status=status.HTTP_400_BAD_REQUEST)

        serializer = VendorLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = request.data.get('email', '')
        response = Response(
            {
                "detail": "OTP sent. Please verify to continue.",
                "masked_identifier": _mask_identifier(identifier),
            },
            status=200,
        )
        # Store the identifier in an HttpOnly cookie so JS cannot read it
        response.set_cookie(
            'otp_identifier',
            identifier,
            max_age=600,            # 10 minutes — matches OTP TTL
            path='/',
            secure=settings.VENDOR_AUTH_COOKIE_SECURE,
            httponly=True,
            samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
        )
        return response

import logging as _logging
_otp_logger = _logging.getLogger('otp')


class VendorOTPVerifyView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request, *args, **kwargs):
        from .models import OTPRecord
        # Identifier is read from the HttpOnly cookie set during login — not from the request body
        email_or_phone = request.COOKIES.get('otp_identifier')
        otp = request.data.get("otp")

        if not email_or_phone or not otp:
            return Response(
                {'detail': 'Session expired or OTP missing. Please log in again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(Q(email__iexact=email_or_phone) | Q(phone=email_or_phone))
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_400_BAD_REQUEST)

        if user.role != 'vendor':
            return Response(
                {'detail': 'OTP verification not required for this user.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch the most recent unused, non-expired OTP record from DB
        otp_record = OTPRecord.objects.filter(
            user=user,
            is_used=False,
            expires_at__gt=timezone.now(),
        ).order_by('-created_at').first()

        if otp_record is None:
            _otp_logger.warning("OTP verify: no valid record for user=%s", user.pk)
            return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        # Validate using constant-time hash comparison (OTP is stored hashed)
        import hmac as _hmac
        from .models import OTPRecord as _OTPRecord
        submitted_hash = _OTPRecord._hash(str(otp).strip())
        if not _hmac.compare_digest(otp_record.otp, submitted_hash):
            _otp_logger.warning("OTP verify: mismatch for user=%s", user.pk)
            return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        # Issue tokens first — if this fails the OTP is still valid for retry
        try:
            refresh = CustomVendorRefreshToken.for_user(user)
            access_token = str(refresh.access_token)
            refresh_token = str(refresh)
        except Exception as exc:
            _otp_logger.error("OTP verify: token issuance failed for user=%s: %s", user.pk, exc)
            return Response(
                {'detail': 'Login failed. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Atomically mark OTP as used now that tokens are ready
        consumed = OTPRecord.objects.filter(pk=otp_record.pk, is_used=False).update(is_used=True)
        if consumed == 0:
            # Concurrent request already consumed this OTP
            _otp_logger.warning("OTP verify: race — OTP already used for user=%s", user.pk)
            return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        _otp_logger.info("OTP verify: success for user=%s", user.pk)

        # Register device session
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
        # Clear the OTP session cookie now that login is complete
        response.delete_cookie('otp_identifier', path='/', domain=settings.VENDOR_AUTH_COOKIE_DOMAIN)
        return response

class VendorOTPResendView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request, *args, **kwargs):
        # Read identifier from HttpOnly cookie — not from request body
        email_or_phone = request.COOKIES.get('otp_identifier')
        if not email_or_phone:
            return Response(
                {'detail': 'Session expired. Please log in again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            user = User.objects.get(Q(email__iexact=email_or_phone) | Q(phone=email_or_phone))
            if user.role != 'vendor':
                return Response({'detail': 'OTP verification not required for this user.'}, status=status.HTTP_400_BAD_REQUEST)

            from .models import OTPRecord
            # Enforce 60-second cooldown between resends
            recent = OTPRecord.objects.filter(
                user=user,
                is_used=False,
                expires_at__gt=timezone.now(),
            ).order_by('-created_at').first()
            if recent:
                cooldown_seconds = 60
                elapsed = (timezone.now() - recent.created_at).total_seconds()
                if elapsed < cooldown_seconds:
                    wait = int(cooldown_seconds - elapsed)
                    return Response(
                        {'detail': f'Please wait {wait} seconds before requesting a new OTP.'},
                        status=status.HTTP_429_TOO_MANY_REQUESTS,
                    )

            otp = otp_token_generator.generate_otp()
            OTPRecord.create_for_user(user, otp)
            recipient = user.email if '@' in email_or_phone else user.phone
            is_email = '@' in email_or_phone
            send_otp.delay(recipient, otp, is_email)
            _otp_logger.info("OTP resend: new OTP issued for user=%s", user.pk)
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

        response.delete_cookie(
            settings.VENDOR_ACCESS_AUTH_COOKIE,
            path=settings.VENDOR_AUTH_COOKIE_PATH,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
            samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
        )
        response.delete_cookie(
            settings.VENDOR_REFRESH_AUTH_COOKIE,
            path=settings.VENDOR_AUTH_COOKIE_PATH,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
            samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
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
            samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
        )
        response.delete_cookie(
            settings.VENDOR_REFRESH_AUTH_COOKIE,
            path=settings.VENDOR_AUTH_COOKIE_PATH,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
            samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
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

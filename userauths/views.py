
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
from vendor.access import IsSellerSignIn

class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    throttle_classes = [RegisterThrottle]

    def post(self, request, *args, **kwargs):
        from .captcha import verify_turnstile
        token = request.data.get('cf_turnstile_response', '')
        ip = get_client_ip(request)
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
        # Clicking the emailed link proves control of the address.
        user.email_verified_at = user.email_verified_at or timezone.now()
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
        ip = get_client_ip(request)
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
            refresh_token = response.data.get("refresh")

            # New access token
            response.set_cookie(
                "access",
                access_token,
                max_age=settings.AUTH_ACCESS_MAX_AGE,
                path=settings.AUTH_COOKIE_PATH,
                secure=settings.AUTH_COOKIE_SECURE,
                httponly=True,
                samesite=settings.AUTH_COOKIE_SAMESITE,
                domain=settings.AUTH_COOKIE_DOMAIN,
            )

            # IMPORTANT:
            # Save the rotated refresh token too
            if refresh_token:
                response.set_cookie(
                    "refresh",
                    refresh_token,
                    max_age=settings.AUTH_REFRESH_MAX_AGE,
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
        elif not request.data.get('token'):
            # The browser drops the access cookie when it expires. Answer 401
            # (not the serializer's 400) so the client refreshes the session.
            return Response(
                {"detail": "Access token missing."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return super().post(request, *args, **kwargs)

class LogoutView(APIView):
    permission_classes = [AllowAny]   # public
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

        # Only this device is logged out. Bumping token_version here would sign
        # the user out everywhere; that's what CustomerLogoutAllView is for.

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


# ── Seller sign-in, step two ──────────────────────────────────────────────────
# After the password checks out we hand the browser a signed, HttpOnly cookie
# naming the account (by id). The OTP step only ever trusts that cookie: a
# plain email in it, as before, let anyone point the OTP step at any seller and
# guess their code without knowing the password. The signature also covers the
# password hash, so changing the password kills every half-finished sign-in.

VENDOR_LOGIN_COOKIE = 'otp_identifier'
VENDOR_LOGIN_SALT = 'userauths.vendor_login'
VENDOR_LOGIN_MAX_AGE = 600              # 10 minutes, same as the code
MAX_RESENDS_PER_LOGIN = 5


def _set_vendor_login_cookie(response, user, channel):
    from django.core import signing
    value = signing.dumps(
        {'uid': user.pk, 'ch': channel, 'pw': user.get_session_auth_hash()[:20]},
        salt=VENDOR_LOGIN_SALT,
    )
    response.set_cookie(
        VENDOR_LOGIN_COOKIE,
        value,
        max_age=VENDOR_LOGIN_MAX_AGE,
        path='/',
        secure=settings.VENDOR_AUTH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
        domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
    )


def _vendor_login_state(request):
    """(user, channel) for a sign-in whose password step passed, else (None, None)."""
    import hmac as _hmac
    from django.core import signing
    raw = request.COOKIES.get(VENDOR_LOGIN_COOKIE)
    if not raw:
        return None, None
    try:
        data = signing.loads(raw, salt=VENDOR_LOGIN_SALT, max_age=VENDOR_LOGIN_MAX_AGE)
        user = User.objects.get(pk=data['uid'])
    except (signing.BadSignature, KeyError, TypeError, ValueError, User.DoesNotExist):
        return None, None
    if not _hmac.compare_digest(str(data.get('pw', '')), user.get_session_auth_hash()[:20]):
        return None, None
    if not user.is_active or getattr(user, 'is_suspended', False) or not user.is_vendor:
        return None, None
    return user, (data.get('ch') if data.get('ch') in ('email', 'sms') else 'email')


def _clear_vendor_login_cookie(response):
    response.delete_cookie(VENDOR_LOGIN_COOKIE, path='/', domain=settings.VENDOR_AUTH_COOKIE_DOMAIN)


def _session_expired():
    response = Response({'detail': 'Session expired. Please log in again.'},
                        status=status.HTTP_400_BAD_REQUEST)
    _clear_vendor_login_cookie(response)
    return response


class VendorTokenObtainPairView(TokenObtainPairView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request):
        from .captcha import verify_turnstile
        token = request.data.get('cf_turnstile_response', '')
        ip = get_client_ip(request)
        if not verify_turnstile(token, ip):
            return Response({'detail': 'CAPTCHA verification failed. Please try again.'}, status=status.HTTP_400_BAD_REQUEST)

        serializer = VendorLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        channel = serializer.validated_data['channel']

        identifier = request.data.get('email', '')
        response = Response(
            {
                "detail": "OTP sent. Please verify to continue.",
                "masked_identifier": _mask_identifier(identifier),
            },
            status=200,
        )
        _set_vendor_login_cookie(response, user, channel)
        return response

import logging as _logging
_otp_logger = _logging.getLogger('otp')


class VendorOTPVerifyView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request, *args, **kwargs):
        from .models import OTPRecord
        from .verification import check_code
        user, _channel = _vendor_login_state(request)
        if user is None:
            return _session_expired()
        otp = request.data.get("otp")
        if not otp:
            return Response({'detail': 'Enter the code we sent you.'}, status=status.HTTP_400_BAD_REQUEST)

        # Capped at verification.MAX_ATTEMPTS wrong guesses per code and
        # consumed atomically on success.
        result = check_code(user, OTPRecord.PURPOSE_VENDOR_LOGIN, otp)
        if not result.ok:
            _otp_logger.warning("OTP verify: %s for user=%s", result.reason, user.pk)
            detail = 'Invalid or expired OTP.' if result.reason == 'invalid' else result.message
            return Response({'detail': detail, 'reason': result.reason}, status=status.HTTP_400_BAD_REQUEST)

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

        _otp_logger.info("OTP verify: success for user=%s", user.pk)

        # Register device session, and tell the seller when it's a device
        # they have never signed in from before.
        try:
            jti = refresh.payload.get('jti')
            if jti:
                from .models import SecurityEvent
                from .session_utils import parse_device_info, record_security_event
                device_name = parse_device_info(request.META.get('HTTP_USER_AGENT', '')[:500])['device_name']
                past_sign_ins = SecurityEvent.objects.filter(
                    user=user, is_vendor=True, event=SecurityEvent.SIGN_IN,
                )
                # Nothing to compare against on the first sign-in we record.
                new_device = (
                    past_sign_ins.exists()
                    and not past_sign_ins.filter(device_name=device_name).exists()
                )
                register_session(user, jti, request, is_vendor=True)
                event = record_security_event(user, SecurityEvent.SIGN_IN, request, new_device=new_device)
                if new_device and event:
                    from .tasks import send_new_sign_in_alert
                    send_new_sign_in_alert.delay(event.pk)
        except Exception:
            pass

        response = Response({'detail': 'Login successful.'}, status=status.HTTP_200_OK)

        # Fire async activity tracking (login event + last_seen update)
        try:
            from vendor.tasks import log_vendor_activity
            from vendor.models import Vendor
            from django_redis import get_redis_connection
            vendor = getattr(user, 'current_vendor', None)
            if vendor:
                ip = get_client_ip(request)
                ua = request.META.get('HTTP_USER_AGENT', '')[:500]
                was_auto_closed = vendor.inactivity_auto_closed
                # Record which team member signed in, not just which store.
                log_vendor_activity.delay(
                    vendor.pk, 'login', ip, ua, {'user_id': user.pk, 'email': user.email},
                )
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
        _clear_vendor_login_cookie(response)
        return response

class VendorOTPResendView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request, *args, **kwargs):
        from django.core.cache import cache
        from .models import OTPRecord
        from .verification import RESEND_COOLDOWN_SECONDS, ResendTooSoon, _pending

        # Only a browser that passed the password step holds a valid cookie.
        user, channel = _vendor_login_state(request)
        if user is None:
            return _session_expired()

        recent = _pending(user, OTPRecord.PURPOSE_VENDOR_LOGIN)
        if recent:
            elapsed = (timezone.now() - recent.created_at).total_seconds()
            if elapsed < RESEND_COOLDOWN_SECONDS:
                wait = int(RESEND_COOLDOWN_SECONDS - elapsed) + 1
                return Response({'detail': str(ResendTooSoon(wait))},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Every new code starts with a fresh wrong-guess counter, so cap the
        # codes too: 5 codes x 5 guesses out of 90,000 per sign-in.
        key = f'vendor_otp_resends:{user.pk}'
        cache.add(key, 0, timeout=VENDOR_LOGIN_MAX_AGE)
        try:
            sent = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=VENDOR_LOGIN_MAX_AGE)
            sent = 1
        if sent > MAX_RESENDS_PER_LOGIN:
            return Response(
                {'detail': 'Too many codes requested. Please sign in again in a few minutes.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        otp = otp_token_generator.generate_otp()
        OTPRecord.create_for_user(user, otp)
        is_email = channel == 'email'
        send_otp.delay(user.email if is_email else user.phone, otp, is_email)
        _otp_logger.info("OTP resend: new OTP issued for user=%s", user.pk)
        return Response({'detail': 'OTP sent to your email or phone.'}, status=status.HTTP_200_OK)

class VendorTokenVerifyView(TokenVerifyView):
    permission_classes = [AllowAny]
    # throttle_scope = "auth_verify"
    
    def post(self, request, *args, **kwargs):
        access_token = request.COOKIES.get('vendor_access')

        if access_token:
            request.data['token'] = access_token

        response = super().post(request, *args, **kwargs)
        # A signed-out device's access token is still well-formed; say so, so
        # the dashboard sends it to the login page instead of looking signed in.
        if response.status_code == 200:
            from rest_framework_simplejwt.tokens import UntypedToken
            from .session_utils import is_session_revoked
            from .tokens import is_seller_token
            token = UntypedToken(request.data.get('token'))
            if not is_seller_token(token):
                # A shopper token is valid, but not for the dashboard.
                return Response({'detail': 'Please sign in to the seller dashboard.', 'code': 'not_seller'},
                                status=status.HTTP_401_UNAUTHORIZED)
            sid = token.get('sid')
            if sid and is_session_revoked(sid):
                return Response(
                    {'detail': 'This device was signed out.', 'code': 'session_revoked'},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
        return response

class VendorLogoutView(APIView):
    permission_classes = [AllowAny]   # public
    def post(self, request, *args, **kwargs):
        # Fire async logout activity before clearing auth
        if request.user.is_authenticated and request.user.is_vendor:
            try:
                from vendor.tasks import log_vendor_activity
                from vendor.models import Vendor
                vendor = getattr(request.user, 'current_vendor', None)
                if vendor:
                    ip = get_client_ip(request)
                    ua = request.META.get('HTTP_USER_AGENT', '')[:500]
                    log_vendor_activity.delay(
                        vendor.pk, 'logout', ip, ua,
                        {'user_id': request.user.pk, 'email': request.user.email},
                    )
                    Vendor.objects.filter(pk=vendor.pk).update(last_logout_at=timezone.now())
            except Exception:
                pass

        # End this device's session for good: a copied refresh cookie must not
        # be able to sign back in after the seller pressed "Log out".
        jti = _get_current_jti(request, settings.VENDOR_REFRESH_AUTH_COOKIE)
        if jti:
            try:
                from .models import SecurityEvent, UserSession
                from .session_utils import record_security_event, revoke_sessions
                revoke_sessions(UserSession.objects.filter(session_key=jti, is_vendor_session=True))
                if request.user.is_authenticated:
                    record_security_event(request.user, SecurityEvent.SIGN_OUT, request)
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
        'kind': 'seller' if session.is_vendor_session else 'shopper',
        'device_type': session.device_type,
        'device_name': session.device_name,
        'browser': session.browser,
        'os': session.os,
        'ip_address': session.ip_address,
        'last_activity': session.last_activity,
        'created_at': session.created_at,
        'is_current': session.session_key == current_jti,
    }


def _current_jtis(request) -> dict:
    """The session keys this browser holds: {'seller': jti, 'shopper': jti}."""
    return {
        'seller': _get_current_jti(request, settings.VENDOR_REFRESH_AUTH_COOKIE),
        'shopper': _get_current_jti(request, 'refresh'),
    }


def _session_current_jti(session, current: dict):
    return current['seller'] if session.is_vendor_session else current['shopper']


def _delete_vendor_cookies(response):
    for name in (settings.VENDOR_ACCESS_AUTH_COOKIE, settings.VENDOR_REFRESH_AUTH_COOKIE):
        response.delete_cookie(
            name,
            path=settings.VENDOR_AUTH_COOKIE_PATH,
            domain=settings.VENDOR_AUTH_COOKIE_DOMAIN,
            samesite=settings.VENDOR_AUTH_COOKIE_SAMESITE,
        )


def _sign_out_other_devices(request, user) -> int:
    """
    Sign out every session of this account except the ones this browser is
    using, on the seller dashboard and the shop alike: it is one account, so a
    stranger signed in on either side has to go.
    """
    from .models import UserSession
    from .session_utils import revoke_sessions
    current = _current_jtis(request)
    keep = [jti for jti in current.values() if jti]
    return revoke_sessions(UserSession.objects.filter(user=user).exclude(session_key__in=keep))


class VendorSessionListView(APIView):
    """
    GET /api/vendor/sessions/
    Every device signed in to this account: seller dashboard sessions first,
    then shopping sessions on negromart.com.
    """
    permission_classes = [IsAuthenticated, IsSellerSignIn]

    def get(self, request):
        from .models import UserSession
        current = _current_jtis(request)
        sessions = UserSession.objects.filter(user=request.user).order_by(
            '-is_vendor_session', '-last_activity'
        )
        return Response([_session_to_dict(s, _session_current_jti(s, current)) for s in sessions])


class VendorSessionRevokeView(APIView):
    """DELETE /api/vendor/sessions/<uuid>/ — sign out one device."""
    permission_classes = [IsAuthenticated, IsSellerSignIn]

    def delete(self, request, session_id):
        from .models import SecurityEvent, UserSession
        from .session_utils import record_security_event, revoke_sessions
        try:
            session = UserSession.objects.get(id=session_id, user=request.user)
        except UserSession.DoesNotExist:
            return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

        if session.session_key == _session_current_jti(session, _current_jtis(request)):
            return Response(
                {'detail': 'This is the device you are using. Use "Log out" instead.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        revoke_sessions([session])
        record_security_event(
            request.user, SecurityEvent.DEVICE_SIGNED_OUT, request,
            target=session.device_name, target_ip=session.ip_address,
            target_kind='seller' if session.is_vendor_session else 'shopper',
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class VendorLogoutAllView(APIView):
    """
    POST /api/vendor/logout-all/
    The "someone else is in my account" button: every token this account holds
    stops working, on every device and on the shop too, this browser included.
    """
    permission_classes = [IsAuthenticated, IsSellerSignIn]

    def post(self, request):
        from .models import SecurityEvent, UserSession
        from .session_utils import record_security_event, revoke_sessions
        # Incrementing token_version immediately invalidates every existing JWT,
        # including ones issued before session tracking existed.
        User.objects.filter(pk=request.user.pk).update(
            token_version=F('token_version') + 1
        )
        count = revoke_sessions(UserSession.objects.filter(user=request.user))
        record_security_event(request.user, SecurityEvent.SIGNED_OUT_EVERYWHERE, request, count=count)

        response = Response({'detail': 'Logged out from all devices.'}, status=status.HTTP_200_OK)
        _delete_vendor_cookies(response)
        # This browser's shopping session is dead too; drop its cookies.
        response.delete_cookie('access', path=settings.AUTH_COOKIE_PATH, domain=settings.AUTH_COOKIE_DOMAIN)
        response.delete_cookie('refresh', path=settings.AUTH_COOKIE_PATH, domain=settings.AUTH_COOKIE_DOMAIN)
        return response


class VendorLogoutOtherSessionsView(APIView):
    """POST /api/vendor/logout-other-sessions/ — keep only this browser signed in."""
    permission_classes = [IsAuthenticated, IsSellerSignIn]

    def post(self, request):
        from .models import SecurityEvent
        from .session_utils import record_security_event
        count = _sign_out_other_devices(request, request.user)
        if count:
            record_security_event(request.user, SecurityEvent.OTHERS_SIGNED_OUT, request, count=count)
        return Response(
            {
                'detail': f'Signed out {count} other device{"" if count == 1 else "s"}.',
                'count': count,
            },
            status=status.HTTP_200_OK,
        )


class VendorChangePasswordView(APIView):
    """
    POST /api/vendor/security/password/
    {current_password, new_password, sign_out_other_devices=true}
    """
    permission_classes = [IsAuthenticated, IsSellerSignIn]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError
        from .models import SecurityEvent
        from .session_utils import record_security_event

        user = request.user
        current = request.data.get('current_password') or ''
        new = request.data.get('new_password') or ''
        sign_out = request.data.get('sign_out_other_devices', True) not in (False, 'false', '0', 0)

        if not user.check_password(current):
            return Response(
                {'current_password': ['Your current password is incorrect.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if current == new:
            return Response(
                {'new_password': ['Your new password must be different from the current one.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            validate_password(new, user)
        except DjangoValidationError as exc:
            return Response({'new_password': list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new)
        user.save(update_fields=['password'])

        count = _sign_out_other_devices(request, user) if sign_out else 0
        record_security_event(user, SecurityEvent.PASSWORD_CHANGED, request, signed_out_devices=count)
        return Response({
            'detail': 'Password changed.',
            'signed_out_devices': count,
            'password_changed_at': user.password_changed_at,
        })


def _mask_phone(phone: str) -> str:
    phone = phone or ''
    return f"{'*' * max(len(phone) - 3, 0)}{phone[-3:]}" if phone else ''


class VendorSecurityOverviewView(APIView):
    """
    GET /api/vendor/security/
    What the Security page needs to show a seller how protected their account
    is: verified contacts, two-step sign-in, password age, signed-in devices
    and recent security activity.
    """
    permission_classes = [IsAuthenticated, IsSellerSignIn]

    def get(self, request):
        from .models import SecurityEvent, UserSession
        user = request.user
        sessions = UserSession.objects.filter(user=user)
        membership = user.vendor_membership
        return Response({
            'first_name': user.first_name,
            'last_name': user.last_name,
            'email': user.email,
            'email_verified': user.is_email_verified,
            'phone': _mask_phone(user.phone),
            'phone_verified': user.is_phone_verified,
            # Every seller sign-in needs a one-time code after the password.
            'two_step_sign_in': True,
            'password_changed_at': user.password_changed_at,
            'store_role': membership.role if membership else None,
            'store_name': membership.vendor.name if membership else None,
            'seller_devices': sessions.filter(is_vendor_session=True).count(),
            'shopper_devices': sessions.filter(is_vendor_session=False).count(),
            'events': [
                {
                    'id': e.pk,
                    'event': e.event,
                    'label': e.get_event_display(),
                    'device_name': e.device_name,
                    'device_type': e.device_type,
                    'ip_address': e.ip_address,
                    'new_device': e.new_device,
                    'metadata': e.metadata,
                    'created_at': e.created_at,
                }
                for e in SecurityEvent.objects.filter(user=user)[:20]
            ],
        })


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

        # Blacklist its refresh token too: deleting the row alone let a copied
        # token sign back in once no other shopper session was left.
        from .session_utils import revoke_sessions
        revoke_sessions([session])
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

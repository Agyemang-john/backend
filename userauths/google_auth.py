"""
userauths/google_auth.py
"Continue with Google" for shoppers, kept separate from email + password sign-up.

The browser gets a Google ID token (Google Identity Services) and posts it
here; we verify it ourselves (signature, audience, issuer, expiry and
email_verified). The Google account is linked by its stable id (`sub`), never by
email, so changing the address on either side doesn't move the link.

One email, one account. What happens when an address is already known:

  linked Google account          → signed in
  password account, not linked   → `link_required`: that account's password,
                                   asked once, proves it's theirs; then linked
  pending (never activated)      → the verified Google owner takes it over;
                                   the old password is wiped (stops anyone
                                   pre-registering someone else's address)
  unknown                        → `needs_profile`: name + phone, then created

Switching methods lives under /api/auth/sign-in-methods/: connect or
disconnect Google, set a password (emailed link) or remove it. An account
always keeps at least one way in, a seller always keeps a password (the
dashboard signs in with password + code), and every change is emailed.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from address import delivery

from .custom_throttles import AnonLoginThrottle, LoginThrottle
from .session_utils import record_security_event, register_session

logger = logging.getLogger(__name__)
User = get_user_model()

PROVIDER = 'google-oauth2'          # social_django's name, so admin/history line up
TICKET_SALT = 'userauths.google.ticket'
TICKET_MAX_AGE = 15 * 60            # the shopper has 15 minutes to finish
GOOGLE_ISSUERS = {'accounts.google.com', 'https://accounts.google.com'}
MAX_FAILED_ATTEMPTS = 5             # same as the password sign-in


class GoogleTokenError(Exception):
    pass


# ── Google token + link helpers ──────────────────────────────────────────────

def client_id():
    return getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', '') or ''


def verify_google_credential(credential):
    """Return {sub, email, first_name, last_name} from a Google ID token, or raise."""
    if not credential or not isinstance(credential, str):
        raise GoogleTokenError('missing')
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        info = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id())
    except ValueError as exc:            # bad signature, wrong audience, expired...
        raise GoogleTokenError(str(exc)) from exc
    except Exception as exc:             # Google's keys unreachable
        logger.warning('Google token verification failed: %s', exc)
        raise GoogleTokenError('unavailable') from exc
    if info.get('iss') not in GOOGLE_ISSUERS:
        raise GoogleTokenError('issuer')
    if not info.get('email') or info.get('email_verified') is not True:
        raise GoogleTokenError('unverified_email')
    return {
        'sub': str(info['sub']),
        'email': info['email'].strip().lower(),
        'first_name': (info.get('given_name') or '').strip()[:128],
        'last_name': (info.get('family_name') or '').strip()[:128],
    }


def google_link(user):
    from social_django.models import UserSocialAuth
    return UserSocialAuth.objects.filter(user=user, provider=PROVIDER).first()


def _link_for_sub(sub):
    from social_django.models import UserSocialAuth
    return UserSocialAuth.objects.select_related('user').filter(provider=PROVIDER, uid=sub).first()


def _create_link(user, google):
    from social_django.models import UserSocialAuth
    return UserSocialAuth.objects.create(
        user=user, provider=PROVIDER, uid=google['sub'],
        extra_data={'email': google['email'], 'linked_at': timezone.now().isoformat()},
    )


def _ticket(google, purpose, user_id=None):
    return signing.dumps({**google, 'purpose': purpose, 'uid': user_id}, salt=TICKET_SALT, compress=True)


def _read_ticket(raw, purpose):
    try:
        data = signing.loads(raw or '', salt=TICKET_SALT, max_age=TICKET_MAX_AGE)
    except signing.SignatureExpired:
        return None, 'ticket_expired'
    except signing.BadSignature:
        return None, 'ticket_invalid'
    if data.get('purpose') != purpose:
        return None, 'ticket_invalid'
    return data, None


def _error(code, detail, http=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({'code': code, 'detail': detail, **extra}, status=http)


def _token_error(exc):
    if str(exc) == 'unverified_email':
        return _error('google_email_unverified',
                      'Your Google account has no verified email address, so it can\'t be used here.')
    if str(exc) == 'unavailable':
        return _error('google_unavailable', 'Google sign-in is unavailable right now. Please try again.',
                      status.HTTP_503_SERVICE_UNAVAILABLE)
    return _error('google_invalid', 'Google sign-in didn\'t go through. Please try again.')


def _not_configured():
    return _error('google_unavailable', 'Google sign-in is not available yet.', status.HTTP_503_SERVICE_UNAVAILABLE)


def _account_state_error(user):
    if getattr(user, 'is_suspended', False):
        return _error('account_suspended', 'Your account has been suspended. Contact support.', status.HTTP_403_FORBIDDEN)
    if not user.is_active:
        return _error('account_inactive', 'Your account is not activated yet. Check your email for the verification link.',
                      status.HTTP_403_FORBIDDEN)
    return None


# ── Session issue (same cookies and claims as the password sign-in) ──────────

def sign_in_response(user, request, payload=None, http=status.HTTP_200_OK):
    refresh = RefreshToken.for_user(user)
    refresh['role'] = user.role
    refresh['is_active'] = user.is_active
    refresh['is_staff'] = user.is_staff
    refresh['token_version'] = user.token_version

    User.objects.filter(pk=user.pk).update(failed_login_attempts=0, lockout_until=None, last_login=timezone.now())
    response = Response({'status': 'signed_in', **(payload or {})}, status=http)
    try:
        register_session(user, refresh['jti'], request, is_vendor=False)
        delivery.sync_cookie(response, request, user)
    except Exception as exc:
        logger.warning('Google sign-in: session bookkeeping failed for %s: %s', user.pk, exc)

    cookie = dict(
        path=settings.AUTH_COOKIE_PATH, secure=settings.AUTH_COOKIE_SECURE,
        httponly=settings.AUTH_COOKIE_HTTP_ONLY, samesite=settings.AUTH_COOKIE_SAMESITE,
        domain=settings.AUTH_COOKIE_DOMAIN,
    )
    response.set_cookie('access', str(refresh.access_token), max_age=settings.AUTH_ACCESS_MAX_AGE, **cookie)
    response.set_cookie('refresh', str(refresh), max_age=settings.AUTH_REFRESH_MAX_AGE, **cookie)
    return response


# ── Security email ───────────────────────────────────────────────────────────

METHOD_CHANGE_TEXT = {
    'google_connected': ('Google sign-in was added to your account',
                         'You can now sign in to Negromart with your Google account {google_email}.'),
    'google_disconnected': ('Google sign-in was removed from your account',
                            'Your Google account {google_email} can no longer be used to sign in. Sign in with your email and password.'),
    'password_removed': ('Your password was removed',
                         'Your account now signs in with Google only ({google_email}).'),
    'password_added': ('A password was added to your account',
                       'You can now sign in with your email and password, as well as with Google.'),
}


def notify_method_change(user, kind, request=None, google_email=''):
    """Email the account owner about a sign-in method change and log it. Never raises."""
    subject, line = METHOD_CHANGE_TEXT[kind]
    line = line.format(google_email=google_email or 'your Google account')
    record_security_event(user, kind, request, is_vendor=False, google_email=google_email)
    try:
        from .tasks import send_sign_in_method_email
        send_sign_in_method_email.delay(user.pk, subject, line)
    except Exception as exc:
        logger.error('Could not queue sign-in method email for %s: %s', user.pk, exc)


# ── Anonymous flow ───────────────────────────────────────────────────────────

class GoogleSignInView(APIView):
    """
    POST /api/auth/google/  {credential}
    → {status: signed_in} (cookies set) | needs_profile | link_required
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request):
        if not client_id():
            return _not_configured()
        try:
            google = verify_google_credential(request.data.get('credential'))
        except GoogleTokenError as exc:
            return _token_error(exc)

        link = _link_for_sub(google['sub'])
        if link is not None:
            user = link.user
            blocked = _account_state_error(user)
            if blocked:
                return blocked
            return sign_in_response(user, request)

        existing = User.objects.filter(email__iexact=google['email']).first()
        if existing is not None and existing.is_active:
            blocked = _account_state_error(existing)
            if blocked:
                return blocked
            if google_link(existing) is not None:
                # Same email, but a *different* Google account is already linked.
                return _error('google_other_account',
                              'This email\'s Negromart account is connected to a different Google account. '
                              'Use that Google account, or sign in with your password.',
                              status.HTTP_409_CONFLICT)
            if not existing.has_usable_password():
                return _error('account_needs_support', 'We couldn\'t sign you in. Contact support.', status.HTTP_409_CONFLICT)
            # A password account: prove it's theirs once, then the two are linked.
            return Response({
                'status': 'link_required',
                'ticket': _ticket(google, 'link', existing.pk),
                'email': existing.email,
            })

        # Brand-new address (or a sign-up that was never activated).
        return Response({
            'status': 'needs_profile',
            'ticket': _ticket(google, 'signup'),
            'profile': {
                'email': google['email'],
                'first_name': google['first_name'] or (existing.first_name if existing else ''),
                'last_name': google['last_name'] or (existing.last_name if existing else ''),
            },
        })


class GoogleCompleteSignupView(APIView):
    """POST /api/auth/google/complete/  {ticket, first_name, last_name, phone}"""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request):
        from .verification import normalize_phone
        google, err = _read_ticket(request.data.get('ticket'), 'signup')
        if err:
            return _error(err, 'That took too long. Please continue with Google again.')

        first = (request.data.get('first_name') or '').strip()[:128]
        last = (request.data.get('last_name') or '').strip()[:128]
        phone = normalize_phone(request.data.get('phone'))
        errors = {}
        if not first:
            errors['first_name'] = ['Enter your first name.']
        if not last:
            errors['last_name'] = ['Enter your last name.']
        if not phone:
            errors['phone'] = ['Enter a valid phone number, e.g. +233241234567.']
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        if _link_for_sub(google['sub']) is not None:
            # Double submit / second tab: this Google account already has one.
            return _error('already_registered', 'This Google account already has a Negromart account. Sign in instead.',
                          status.HTTP_409_CONFLICT)

        try:
            with transaction.atomic():
                user = User.objects.select_for_update().filter(email__iexact=google['email']).first()
                if user is not None and getattr(user, 'is_suspended', False):
                    return _error('account_suspended', 'Your account has been suspended. Contact support.',
                                  status.HTTP_403_FORBIDDEN)
                if user is not None and user.is_active:
                    return _error('email_exists', 'An account with this email already exists. Sign in instead.',
                                  status.HTTP_409_CONFLICT)
                if User.objects.filter(phone=phone).exclude(pk=getattr(user, 'pk', None)).exists():
                    return Response({'phone': ['An account with this phone number already exists. Sign in instead.']},
                                    status=status.HTTP_400_BAD_REQUEST)
                now = timezone.now()
                if user is None:
                    user = User(email=google['email'], role='customer')
                else:
                    # A sign-up that was never activated. The verified Google
                    # owner takes it over, and whoever typed that password in
                    # can no longer use it.
                    user.token_version = F('token_version') + 1
                user.first_name, user.last_name, user.phone = first, last, phone
                user.set_unusable_password()
                user.is_active = True
                user.email_verified_at = now
                user.auth_provider = 'google'
                user.save()
                user.refresh_from_db()
                _create_link(user, google)
        except IntegrityError:
            return _error('email_exists', 'An account with these details already exists. Sign in instead.',
                          status.HTTP_409_CONFLICT)

        record_security_event(user, 'account_created_google', request, is_vendor=False, google_email=google['email'])
        return sign_in_response(user, request, {'created': True}, http=status.HTTP_201_CREATED)


class GoogleLinkView(APIView):
    """POST /api/auth/google/link/  {ticket, password}: connect Google to an existing password account."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle, AnonLoginThrottle]

    def post(self, request):
        from .serializers import LOCKOUT_TIME
        google, err = _read_ticket(request.data.get('ticket'), 'link')
        if err:
            return _error(err, 'That took too long. Please continue with Google again.')
        user = User.objects.filter(pk=google.get('uid')).first()
        if user is None or user.email.lower() != google['email']:
            return _error('ticket_invalid', 'Please continue with Google again.')
        blocked = _account_state_error(user)
        if blocked:
            return blocked

        if user.lockout_until and user.lockout_until > timezone.now():
            return _error('locked', f"Too many failed attempts. Try again after {timezone.localtime(user.lockout_until):%H:%M}.",
                          status.HTTP_429_TOO_MANY_REQUESTS)
        if not user.check_password(request.data.get('password') or ''):
            User.objects.filter(pk=user.pk).update(failed_login_attempts=F('failed_login_attempts') + 1)
            user.refresh_from_db(fields=['failed_login_attempts'])
            if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                User.objects.filter(pk=user.pk).update(lockout_until=timezone.now() + LOCKOUT_TIME)
            return _error('wrong_password', 'That password isn\'t right. Try again, or reset it.')

        if google_link(user) is None:
            if _link_for_sub(google['sub']) is not None:
                return _error('google_in_use', 'This Google account is already connected to another Negromart account.',
                              status.HTTP_409_CONFLICT)
            try:
                _create_link(user, google)
            except IntegrityError:
                return _error('google_in_use', 'This Google account is already connected to another Negromart account.',
                              status.HTTP_409_CONFLICT)
            notify_method_change(user, 'google_connected', request, google['email'])
        return sign_in_response(user, request, {'linked': True})


# ── Signed-in: sign-in methods ───────────────────────────────────────────────

class MethodChangeThrottle(UserRateThrottle):
    scope = 'sign_in_method_change'
    rate = '10/hour'


def methods_payload(user):
    link = google_link(user)
    has_password = user.has_usable_password()
    is_seller = bool(user.vendor_membership)
    return {
        'email': user.email,
        'has_password': has_password,
        'password_changed_at': user.password_changed_at,
        'google': {
            'connected': link is not None,
            'email': (link.extra_data or {}).get('email', '') if link else '',
            'linked_at': (link.extra_data or {}).get('linked_at') if link else None,
        },
        'is_seller': is_seller,
        'can_remove_password': has_password and link is not None and not is_seller,
        'can_disconnect_google': link is not None and has_password,
        'google_available': bool(client_id()),
    }


def _check_current_password(user, password):
    """None when the password is right (or the account has none to check)."""
    if not user.has_usable_password():
        return None
    if not password:
        return _error('password_required', 'Enter your current password to confirm.')
    if not user.check_password(password):
        return _error('wrong_password', 'That password isn\'t right.')
    return None


class SignInMethodsView(APIView):
    """GET /api/auth/sign-in-methods/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(methods_payload(request.user))


class ConnectGoogleView(APIView):
    """POST /api/auth/sign-in-methods/google/connect/  {credential, password}"""
    permission_classes = [IsAuthenticated]
    throttle_classes = [MethodChangeThrottle]

    def post(self, request):
        user = request.user
        if not client_id():
            return _not_configured()
        if google_link(user) is not None:
            return _error('already_connected', 'A Google account is already connected. Disconnect it first.',
                          status.HTTP_409_CONFLICT)
        bad = _check_current_password(user, request.data.get('password'))
        if bad:
            return bad
        try:
            google = verify_google_credential(request.data.get('credential'))
        except GoogleTokenError as exc:
            return _token_error(exc)
        if _link_for_sub(google['sub']) is not None:
            return _error('google_in_use', 'This Google account is already connected to another Negromart account.',
                          status.HTTP_409_CONFLICT)
        try:
            _create_link(user, google)
        except IntegrityError:
            return _error('google_in_use', 'This Google account is already connected to another Negromart account.',
                          status.HTTP_409_CONFLICT)
        notify_method_change(user, 'google_connected', request, google['email'])
        return Response(methods_payload(user))


class DisconnectGoogleView(APIView):
    """POST /api/auth/sign-in-methods/google/disconnect/  {password}"""
    permission_classes = [IsAuthenticated]
    throttle_classes = [MethodChangeThrottle]

    def post(self, request):
        user = request.user
        link = google_link(user)
        if link is None:
            return Response(methods_payload(user))
        if not user.has_usable_password():
            return _error('last_method', 'Set a password first, otherwise you couldn\'t sign in any more.',
                          status.HTTP_409_CONFLICT)
        bad = _check_current_password(user, request.data.get('password'))
        if bad:
            return bad
        google_email = (link.extra_data or {}).get('email', '')
        link.delete()
        User.objects.filter(pk=user.pk, auth_provider='google').update(auth_provider='email')
        notify_method_change(user, 'google_disconnected', request, google_email)
        return Response(methods_payload(user))


class RemovePasswordView(APIView):
    """POST /api/auth/sign-in-methods/password/remove/  {password}: sign in with Google only."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [MethodChangeThrottle]

    def post(self, request):
        user = request.user
        link = google_link(user)
        if link is None:
            return _error('last_method', 'Connect Google first, otherwise you couldn\'t sign in any more.',
                          status.HTTP_409_CONFLICT)
        if user.vendor_membership:
            return _error('seller_needs_password',
                          'Seller accounts need a password: the seller dashboard signs in with a password and a code.',
                          status.HTTP_409_CONFLICT)
        if not user.has_usable_password():
            return Response(methods_payload(user))
        bad = _check_current_password(user, request.data.get('password'))
        if bad:
            return bad
        user.set_unusable_password()
        user.auth_provider = 'google'
        user.save(update_fields=['password', 'auth_provider'])
        notify_method_change(user, 'password_removed', request, (link.extra_data or {}).get('email', ''))
        return Response(methods_payload(user))


class SendPasswordSetupLinkView(APIView):
    """
    POST /api/auth/sign-in-methods/password/setup-link/
    Google-only account: email a link to set a password (the same link as
    "Forgot password", which proves the inbox is theirs).
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [MethodChangeThrottle]

    def post(self, request):
        user = request.user
        if user.has_usable_password():
            return _error('has_password', 'Your account already has a password. Use Change password instead.',
                          status.HTTP_409_CONFLICT)
        send_password_link(request, user)
        return Response({'detail': 'sent', 'email': user.email})


def send_password_link(request, user):
    """Send djoser's reset-password email, which also works to set a first password."""
    from djoser.conf import settings as djoser_settings
    from djoser.compat import get_user_email
    djoser_settings.EMAIL.password_reset(request, {'user': user}).send([get_user_email(user)])

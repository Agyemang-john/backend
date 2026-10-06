"""
userauths/seller_signup_views.py
Create-and-verify a Negromart account from inside the seller registration flow.

Before this, someone without an account had to leave seller.negromart.com,
register on the main site, click an email link, and come back. Now the seller
site does it inline, Amazon-style:

    1. POST seller-signup/start/         name, email, phone, password, captcha
                                         → account created INACTIVE, code emailed
    2. POST seller-signup/verify-email/  the 5-digit code from the email
                                         → email verified, code texted to phone
    3. POST seller-signup/verify-phone/  code from the SMS
                                         → phone verified, account ACTIVE, and the
                                           user is signed in as a customer (same
                                           cookies as a normal login), so the
                                           existing 4-step store application works
                                           unchanged.

    GET  seller-signup/state/            resume after a reload / closed tab
    POST seller-signup/resend/           new code for the current step (60 s cooldown)
    POST seller-signup/change-phone/     fix a mistyped number at step 3

Progress between steps is carried in a short-lived, signed, HttpOnly cookie
(`seller_signup`) holding only the user id, so the browser never handles an
identifier it could tamper with, and there is no server-side session to store.

The result is an ordinary customer account (one identity for shopping and
selling). Existing customers skip all this: they sign in, and if their phone
was never verified they complete only step 3 via phone/verification/*.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, transaction
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from . import verification as v
from .serializers import RegisterSerializer
from .session_utils import register_session

User = get_user_model()
logger = logging.getLogger(__name__)

SIGNUP_COOKIE = 'seller_signup'
SIGNUP_COOKIE_SALT = 'userauths.seller_signup'
SIGNUP_MAX_AGE = 60 * 60  # one hour to finish both verification steps

STEP_VERIFY_EMAIL = 'verify_email'
STEP_VERIFY_PHONE = 'verify_phone'
STEP_DONE = 'done'


# ── Throttles ─────────────────────────────────────────────────────────────────
# Rates are set on the class (like custom_throttles.py), so no settings change
# is needed. They sit on top of the per-code attempt cap and resend cooldown.

class SellerSignupStartThrottle(AnonRateThrottle):
    scope = 'seller_signup'
    rate = '10/hour'


class VerificationAnonThrottle(AnonRateThrottle):
    scope = 'verification_anon'
    rate = '30/hour'


class VerificationUserThrottle(UserRateThrottle):
    scope = 'verification_user'
    rate = '20/hour'


# ── Helpers ───────────────────────────────────────────────────────────────────

class SellerAccountSerializer(RegisterSerializer):
    """
    RegisterSerializer's validation (unique email/phone, strong password),
    minus the activation-link email: this flow verifies with codes instead.
    """

    def validate_phone(self, value):
        phone = v.normalize_phone(value)
        if not phone:
            raise serializers.ValidationError(
                "Enter a valid phone number with country code, e.g. +233241234567."
            )
        return super().validate_phone(phone)

    def validate_email(self, value):
        return super().validate_email(value.strip().lower())

    def create(self, validated_data):
        password = validated_data.pop('password')
        # create_user leaves is_active=False until both codes are confirmed.
        return User.objects.create_user(**validated_data, password=password)


def _client_ip(request):
    return (
        request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
        or request.META.get('REMOTE_ADDR')
    )


def _set_signup_cookie(response, user):
    response.set_cookie(
        SIGNUP_COOKIE,
        signing.dumps({'uid': user.pk}, salt=SIGNUP_COOKIE_SALT),
        max_age=SIGNUP_MAX_AGE,
        path='/',
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        domain=settings.AUTH_COOKIE_DOMAIN,
    )


def _clear_signup_cookie(response):
    response.delete_cookie(SIGNUP_COOKIE, path='/', domain=settings.AUTH_COOKIE_DOMAIN)


def _signup_user(request):
    """The pending (not yet active) account this browser is verifying, or None."""
    raw = request.COOKIES.get(SIGNUP_COOKIE)
    if not raw:
        return None
    try:
        data = signing.loads(raw, salt=SIGNUP_COOKIE_SALT, max_age=SIGNUP_MAX_AGE)
    except signing.BadSignature:
        return None
    # Only inactive accounts can be driven through this flow; once active the
    # user must sign in with their password like everyone else.
    return User.objects.filter(pk=data.get('uid'), is_active=False).first()


def _expired_response():
    response = Response(
        {'detail': 'Your sign-up session has expired. Please start again.', 'code': 'signup_expired'},
        status=status.HTTP_404_NOT_FOUND,
    )
    _clear_signup_cookie(response)
    return response


def _step_for(user):
    if not user.is_email_verified:
        return STEP_VERIFY_EMAIL
    if not user.is_phone_verified:
        return STEP_VERIFY_PHONE
    return STEP_DONE


def _state_payload(user):
    return {
        'step': _step_for(user),
        'first_name': user.first_name,
        'masked_email': v.mask_email(user.email),
        'masked_phone': v.mask_phone(user.phone),
        'resend_cooldown_seconds': v.RESEND_COOLDOWN_SECONDS,
    }


def _send_quietly(send, *args, **kwargs):
    """Send a code, but if one went out seconds ago just keep using that one."""
    try:
        send(*args, **kwargs)
    except v.ResendTooSoon:
        pass


def _login_customer(response, user, request):
    """
    Sign the freshly verified user in on the main site's customer cookies, with
    the same claims and device-session bookkeeping as CustomTokenObtainPairView.
    """
    refresh = RefreshToken.for_user(user)
    refresh['role'] = user.role
    refresh['is_active'] = user.is_active
    refresh['is_staff'] = user.is_staff
    refresh['token_version'] = user.token_version
    try:
        register_session(user, refresh.payload.get('jti'), request, is_vendor=False)
    except Exception:
        logger.exception("seller signup: could not register session for user=%s", user.pk)

    common = dict(
        path=settings.AUTH_COOKIE_PATH,
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=settings.AUTH_COOKIE_HTTP_ONLY,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        domain=settings.AUTH_COOKIE_DOMAIN,
    )
    response.set_cookie('access', str(refresh.access_token), max_age=settings.AUTH_ACCESS_MAX_AGE, **common)
    response.set_cookie('refresh', str(refresh), max_age=settings.AUTH_REFRESH_MAX_AGE, **common)


def _cooldown_response(exc):
    return Response(
        {'detail': str(exc), 'code': 'resend_too_soon', 'wait_seconds': exc.wait_seconds},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


def _code_error(result):
    return Response(
        {'detail': result.message, 'code': f'code_{result.reason}'},
        status=status.HTTP_400_BAD_REQUEST,
    )


# ── Inline signup ─────────────────────────────────────────────────────────────

class SellerSignupStartView(APIView):
    """POST /api/seller-signup/start/"""
    permission_classes = [AllowAny]
    authentication_classes = []  # anonymous by design; ignore any stale cookies
    throttle_classes = [SellerSignupStartThrottle]

    ACCOUNT_EXISTS = {
        'detail': 'An account with this email already exists. Sign in to continue.',
        'code': 'account_exists',
    }

    def post(self, request):
        from .captcha import verify_turnstile
        if not verify_turnstile(request.data.get('cf_turnstile_response', ''), _client_ip(request)):
            return Response(
                {'detail': 'CAPTCHA verification failed. Please try again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = (request.data.get('email') or '').strip().lower()
        password = request.data.get('password') or ''
        existing = User.objects.filter(email__iexact=email).first() if email else None

        if existing is not None:
            user = self._resume(existing, password, request.data)
            if isinstance(user, Response):
                return user
        else:
            # dict(items()) flattens both JSON bodies and form QueryDicts.
            data = dict(request.data.items())
            data['email'] = email
            serializer = SellerAccountSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            try:
                with transaction.atomic():
                    user = serializer.save()
            except IntegrityError:
                # Lost a race with another signup for the same email/phone.
                return Response(self.ACCOUNT_EXISTS, status=status.HTTP_409_CONFLICT)

        _send_quietly(v.send_email_code, user)
        response = Response(_state_payload(user), status=status.HTTP_201_CREATED)
        _set_signup_cookie(response, user)
        return response

    def _resume(self, existing, password, data):
        """
        Same email again. An active account must sign in normally. An
        unverified one (abandoned half-way) may resume, but only with the
        password it was created with, so nobody can hijack someone else's
        pending signup. Either way the reply is identical for a wrong password
        and an active account, so it reveals nothing extra.
        """
        if existing.is_active or not existing.check_password(password):
            return Response(self.ACCOUNT_EXISTS, status=status.HTTP_409_CONFLICT)

        # Allow correcting name/phone typos made the first time round.
        phone = v.normalize_phone(data.get('phone')) or existing.phone
        if phone != existing.phone:
            if User.objects.filter(phone=phone).exclude(pk=existing.pk).exists():
                return Response(
                    {'phone': ['An account with this phone number already exists. Login or reset password']},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            existing.phone = phone
            existing.phone_verified_at = None
        existing.first_name = (data.get('first_name') or existing.first_name).strip()
        existing.last_name = (data.get('last_name') or existing.last_name).strip()
        existing.save(update_fields=['phone', 'phone_verified_at', 'first_name', 'last_name'])
        return existing


class SellerSignupStateView(APIView):
    """GET /api/seller-signup/state/: which step this browser is on."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        user = _signup_user(request)
        if user is None:
            return _expired_response()
        return Response(_state_payload(user))


class SellerSignupVerifyEmailView(APIView):
    """POST /api/seller-signup/verify-email/  {code}"""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [VerificationAnonThrottle]

    def post(self, request):
        user = _signup_user(request)
        if user is None:
            return _expired_response()

        if not user.is_email_verified:
            result = v.confirm_email(user, request.data.get('code'))
            if not result.ok:
                return _code_error(result)

        if not user.is_phone_verified:
            _send_quietly(v.send_phone_code, user)
        return Response(_state_payload(user))


class SellerSignupVerifyPhoneView(APIView):
    """POST /api/seller-signup/verify-phone/  {code}: final step, signs the user in."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [VerificationAnonThrottle]

    def post(self, request):
        user = _signup_user(request)
        if user is None:
            return _expired_response()
        if not user.is_email_verified:
            return Response(
                {'detail': 'Please verify your email first.', 'code': 'email_unverified'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user.is_phone_verified:
            try:
                result = v.confirm_phone(user, request.data.get('code'))
            except v.PhoneTakenError:
                return Response(
                    {'detail': 'This phone number is now used by another account. Please use a different number.',
                     'code': 'phone_taken'},
                    status=status.HTTP_409_CONFLICT,
                )
            if not result.ok:
                return _code_error(result)

        # Both contacts proven: the account goes live and is signed in.
        user.is_active = True
        user.save(update_fields=['is_active'])

        response = Response({**_state_payload(user), 'step': STEP_DONE})
        _login_customer(response, user, request)
        _clear_signup_cookie(response)
        return response


class SellerSignupResendView(APIView):
    """POST /api/seller-signup/resend/: new code for whichever step is pending."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [VerificationAnonThrottle]

    def post(self, request):
        user = _signup_user(request)
        if user is None:
            return _expired_response()
        step = _step_for(user)
        try:
            if step == STEP_VERIFY_EMAIL:
                v.send_email_code(user)
            elif step == STEP_VERIFY_PHONE:
                v.send_phone_code(user)
        except v.ResendTooSoon as exc:
            return _cooldown_response(exc)
        return Response(_state_payload(user))


class SellerSignupChangePhoneView(APIView):
    """POST /api/seller-signup/change-phone/  {phone}: fix a typo before verifying."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [VerificationAnonThrottle]

    def post(self, request):
        user = _signup_user(request)
        if user is None:
            return _expired_response()
        if _step_for(user) != STEP_VERIFY_PHONE:
            return Response({'detail': 'Your phone number cannot be changed at this step.'},
                            status=status.HTTP_400_BAD_REQUEST)

        phone = v.normalize_phone(request.data.get('phone'))
        if not phone:
            return Response({'phone': ['Enter a valid phone number with country code, e.g. +233241234567.']},
                            status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(phone=phone).exclude(pk=user.pk).exists():
            return Response({'phone': ['An account with this phone number already exists.']},
                            status=status.HTTP_400_BAD_REQUEST)

        user.phone = phone
        user.save(update_fields=['phone'])
        try:
            # A new number gets its own code. The cooldown still applies, so
            # this endpoint cannot be used to spam SMS.
            v.send_phone_code(user)
        except v.ResendTooSoon as exc:
            return _cooldown_response(exc)
        return Response(_state_payload(user))


# ── Phone verification for existing, signed-in customers ─────────────────────

class PhoneVerificationSendView(APIView):
    """
    POST /api/phone/verification/send/  {phone?}

    Texts a code to the customer's number, or to a new number they want to use
    instead. The number on file only changes once the code is confirmed.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [VerificationUserThrottle]

    def post(self, request):
        user = request.user
        requested = request.data.get('phone')
        phone = v.normalize_phone(requested or user.phone)
        if not phone:
            return Response(
                {'phone': ['Enter a valid phone number with country code, e.g. +233241234567.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if phone != user.phone and User.objects.filter(phone=phone).exclude(pk=user.pk).exists():
            return Response({'phone': ['An account with this phone number already exists.']},
                            status=status.HTTP_400_BAD_REQUEST)
        if phone == user.phone and user.is_phone_verified:
            return Response({'detail': 'Your phone number is already verified.', 'phone_verified': True})

        try:
            v.send_phone_code(user, phone=phone)
        except v.ResendTooSoon as exc:
            return _cooldown_response(exc)
        return Response({
            'detail': 'Verification code sent.',
            'masked_phone': v.mask_phone(phone),
            'resend_cooldown_seconds': v.RESEND_COOLDOWN_SECONDS,
        })


class PhoneVerificationConfirmView(APIView):
    """POST /api/phone/verification/confirm/  {code}"""
    permission_classes = [IsAuthenticated]
    throttle_classes = [VerificationUserThrottle]

    def post(self, request):
        try:
            result = v.confirm_phone(request.user, request.data.get('code'))
        except v.PhoneTakenError:
            return Response(
                {'detail': 'This phone number is now used by another account. Please use a different number.',
                 'code': 'phone_taken'},
                status=status.HTTP_409_CONFLICT,
            )
        if not result.ok:
            return _code_error(result)
        return Response({
            'detail': 'Phone number verified.',
            'phone_verified': True,
            'masked_phone': v.mask_phone(request.user.phone),
        })

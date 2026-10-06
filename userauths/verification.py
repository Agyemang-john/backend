"""
userauths/verification.py
One-time-code verification of a user's email address and phone number.

Modelled on Amazon's account/seller onboarding:
  - new account  → a code is emailed; entering it proves the address
  - seller setup → a code is texted; entering it proves the phone number

Used by the inline seller signup (seller_signup_views.py) and by the
"verify your phone" step that existing customers complete before opening a
store. Codes live in OTPRecord (hashed, purpose-scoped, 10-minute TTL).

Abuse controls, all enforced here so every caller gets them:
  - RESEND_COOLDOWN_SECONDS between codes for the same purpose
  - MAX_ATTEMPTS wrong guesses burn the code (5-digit codes are guessable otherwise)
  - views add DRF throttles on top (per IP / per user)
"""

import hmac
import logging
import re
from dataclasses import dataclass

from django.db.models import F
from django.utils import timezone

from .models import OTPRecord
from .otp import otp_token_generator

logger = logging.getLogger('otp')

OTP_TTL_MINUTES = 10
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60

CHANNEL_EMAIL = 'email'
CHANNEL_SMS = 'sms'

_PURPOSE_CHANNEL = {
    OTPRecord.PURPOSE_EMAIL_VERIFY: CHANNEL_EMAIL,
    OTPRecord.PURPOSE_PHONE_VERIFY: CHANNEL_SMS,
}

# E.164-style: optional '+', 9–15 digits. Same shape Vendor.contact accepts.
_PHONE_RE = re.compile(r'^\+?\d{9,15}$')


class ResendTooSoon(Exception):
    def __init__(self, wait_seconds):
        self.wait_seconds = wait_seconds
        super().__init__(f"Please wait {wait_seconds} seconds before requesting a new code.")


@dataclass
class CheckResult:
    ok: bool
    record: OTPRecord | None = None
    # 'invalid' (wrong code), 'expired' (none pending), 'locked' (too many tries)
    reason: str = ''

    @property
    def message(self):
        return {
            'invalid': "That code is incorrect. Please check it and try again.",
            'expired': "Your code has expired. Request a new one.",
            'locked': "Too many incorrect attempts. Request a new code.",
        }.get(self.reason, "Verification failed.")


def normalize_phone(raw):
    """Strip spaces/dashes/brackets; return None if it isn't a plausible number."""
    if not raw:
        return None
    phone = re.sub(r'[\s\-().]', '', str(raw))
    return phone if _PHONE_RE.match(phone) else None


def mask_email(email):
    local, _, domain = (email or '').partition('@')
    return f"{local[:1]}***@{domain}" if domain else '***'


def mask_phone(phone):
    phone = phone or ''
    return f"***{phone[-3:]}" if len(phone) >= 3 else '***'


def _pending(user, purpose):
    return (
        OTPRecord.objects
        .filter(user=user, purpose=purpose, is_used=False, expires_at__gt=timezone.now())
        .order_by('-created_at')
        .first()
    )


def issue_code(user, purpose, target, *, enforce_cooldown=True):
    """
    Create a fresh code for `purpose`, send it to `target`, and return the record.

    Raises ResendTooSoon when a code for the same purpose was sent within the
    cooldown window. Sending happens on Celery, so a slow SMS/email provider
    never blocks the request.
    """
    if enforce_cooldown:
        recent = _pending(user, purpose)
        if recent:
            elapsed = (timezone.now() - recent.created_at).total_seconds()
            if elapsed < RESEND_COOLDOWN_SECONDS:
                raise ResendTooSoon(int(RESEND_COOLDOWN_SECONDS - elapsed) + 1)

    code = otp_token_generator.generate_otp()
    record = OTPRecord.create_for_user(
        user, code, ttl_minutes=OTP_TTL_MINUTES, purpose=purpose, target=target,
    )

    from .tasks import send_verification_code
    send_verification_code.delay(target, code, _PURPOSE_CHANNEL[purpose], user.first_name)
    logger.info("verification: issued %s code for user=%s", purpose, user.pk)
    return record


def check_code(user, purpose, submitted):
    """
    Validate `submitted` against the user's pending code for `purpose`.

    On success the code is consumed atomically (a concurrent second submit of
    the same code fails). On failure the attempt counter is bumped; reaching
    MAX_ATTEMPTS burns the code so the user must request a new one.
    """
    record = _pending(user, purpose)
    if record is None:
        return CheckResult(False, reason='expired')
    if record.attempts >= MAX_ATTEMPTS:
        OTPRecord.objects.filter(pk=record.pk).update(is_used=True)
        return CheckResult(False, reason='locked')

    submitted_hash = OTPRecord._hash(str(submitted or '').strip())
    if not hmac.compare_digest(record.otp, submitted_hash):
        OTPRecord.objects.filter(pk=record.pk).update(attempts=F('attempts') + 1)
        if record.attempts + 1 >= MAX_ATTEMPTS:
            OTPRecord.objects.filter(pk=record.pk).update(is_used=True)
            return CheckResult(False, reason='locked')
        return CheckResult(False, reason='invalid')

    consumed = OTPRecord.objects.filter(pk=record.pk, is_used=False).update(is_used=True)
    if not consumed:
        return CheckResult(False, reason='expired')
    logger.info("verification: %s confirmed for user=%s", purpose, user.pk)
    return CheckResult(True, record=record)


# ── High-level helpers ────────────────────────────────────────────────────────

def send_email_code(user, **kwargs):
    return issue_code(user, OTPRecord.PURPOSE_EMAIL_VERIFY, user.email, **kwargs)


def send_phone_code(user, phone=None, **kwargs):
    """Text a code to `phone` (defaults to the number on file)."""
    return issue_code(user, OTPRecord.PURPOSE_PHONE_VERIFY, phone or user.phone, **kwargs)


def confirm_email(user, submitted):
    result = check_code(user, OTPRecord.PURPOSE_EMAIL_VERIFY, submitted)
    if result.ok:
        # The code went to user.email; an address change would have issued a new one.
        user.email_verified_at = timezone.now()
        user.save(update_fields=['email_verified_at'])
    return result


class PhoneTakenError(Exception):
    """The number was claimed by another account between sending and confirming."""


def confirm_phone(user, submitted):
    """
    Confirm a phone code. If the code was sent to a new number, that number
    becomes the user's phone now that they've proven they control it.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    result = check_code(user, OTPRecord.PURPOSE_PHONE_VERIFY, submitted)
    if not result.ok:
        return result

    target = result.record.target or user.phone
    fields = ['phone_verified_at']
    if target != user.phone:
        if User.objects.filter(phone=target).exclude(pk=user.pk).exists():
            raise PhoneTakenError(target)
        user.phone = target
        fields.append('phone')
    user.phone_verified_at = timezone.now()
    user.save(update_fields=fields)
    return result

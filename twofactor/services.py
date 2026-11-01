"""TOTP setup, verification (with replay protection) and recovery codes."""
import hashlib
import hmac
import secrets
import time

import pyotp
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import TOTPDevice

ISSUER = 'Negromart'
RECOVERY_CODES = 10
MAX_FAILURES = 5          # per 15 minutes per user
WINDOW = 1                # accept the previous/next 30 s step (clock drift)


class TwoFactorError(Exception):
    pass


def _fernet():
    return Fernet(settings.FERNET_KEY.encode() if isinstance(settings.FERNET_KEY, str) else settings.FERNET_KEY)


def _secret(device):
    return _fernet().decrypt(device.secret_encrypted.encode()).decode()


def _hash(code):
    return hmac.new(settings.SECRET_KEY.encode(), code.replace('-', '').upper().encode(), hashlib.sha256).hexdigest()


def is_enabled(user):
    return TOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).exists()


def start_setup(user):
    """(secret, otpauth_uri, qr_svg). Replaces any unconfirmed attempt; a confirmed device must be disabled first."""
    if is_enabled(user):
        raise TwoFactorError("An authenticator is already set up. Turn it off first to replace it.")
    secret = pyotp.random_base32()
    TOTPDevice.objects.update_or_create(user=user, defaults={
        'secret_encrypted': _fernet().encrypt(secret.encode()).decode(), 'confirmed_at': None, 'last_step': 0,
        'recovery_hashes': []})
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email or str(user.pk), issuer_name=ISSUER)
    return secret, uri, qr_svg(uri)


def qr_svg(uri):
    import io

    import segno
    buf = io.BytesIO()
    segno.make(uri, error='m').save(buf, kind='svg', scale=5, border=2, xmldecl=False, svgns=True)
    return buf.getvalue().decode()


def _throttled(user):
    return (cache.get(f"2fa:fail:{user.pk}") or 0) >= MAX_FAILURES


def _fail(user):
    key = f"2fa:fail:{user.pk}"
    if not cache.add(key, 1, 900):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, 900)


def _check_totp(device, code):
    """Returns the matched time step, or None. Rejects steps already used."""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(_secret(device))
    now_step = int(time.time()) // 30
    for step in range(now_step - WINDOW, now_step + WINDOW + 1):
        if step <= device.last_step:
            continue
        if hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


@transaction.atomic
def confirm(user, code):
    """Finish setup with a code from the app. Returns the recovery codes (shown once)."""
    device = TOTPDevice.objects.select_for_update().filter(user=user, confirmed_at__isnull=True).first()
    if device is None:
        raise TwoFactorError("Start the setup again.")
    step = _check_totp(device, code)
    if step is None:
        _fail(user)
        raise TwoFactorError("That code didn't match. Check your phone's time is set automatically.")
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}".upper() for _ in range(RECOVERY_CODES)]
    device.recovery_hashes = [_hash(c) for c in codes]
    device.confirmed_at, device.last_step = timezone.now(), step
    device.save()
    _security_event(user, 'two_factor_on')
    return codes


@transaction.atomic
def verify(user, code):
    """True when `code` is a fresh authenticator code or an unused recovery code."""
    if _throttled(user):
        raise TwoFactorError("Too many wrong codes. Wait 15 minutes and try again.")
    device = TOTPDevice.objects.select_for_update().filter(user=user, confirmed_at__isnull=False).first()
    if device is None:
        return False
    step = _check_totp(device, code)
    if step is not None:
        device.last_step = step
        device.save(update_fields=['last_step'])
        return True
    h = _hash(str(code or '').strip())
    if h in device.recovery_hashes:
        device.recovery_hashes = [x for x in device.recovery_hashes if x != h]
        device.save(update_fields=['recovery_hashes'])
        _security_event(user, 'two_factor_recovery_used')
        return True
    _fail(user)
    return False


def disable(user, code):
    if not verify(user, code):
        raise TwoFactorError("That code didn't match.")
    TOTPDevice.objects.filter(user=user).delete()
    _security_event(user, 'two_factor_off')


def recovery_left(user):
    device = TOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).only('recovery_hashes').first()
    return len(device.recovery_hashes) if device else 0


def _security_event(user, what):
    import logging
    logging.getLogger('security').info("2fa %s for user %s", what, user.pk)


@transaction.atomic
def regenerate_recovery(user, code):
    """New set of recovery codes (old ones stop working). Needs a current authenticator code."""
    if not verify(user, code):
        raise TwoFactorError("That code didn't match.")
    device = TOTPDevice.objects.select_for_update().get(user=user)
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}".upper() for _ in range(RECOVERY_CODES)]
    device.recovery_hashes = [_hash(c) for c in codes]
    device.save(update_fields=['recovery_hashes'])
    _security_event(user, 'two_factor_recovery_regenerated')
    return codes


def reset(user):
    """Remove the authenticator (support/lockout). The user sets it up again on next sign-in."""
    TOTPDevice.objects.filter(user=user).delete()
    _security_event(user, 'two_factor_reset')

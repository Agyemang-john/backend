"""
Helpers for device-aware session tracking.

Requires: pip install user-agents
"""
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def get_client_ip(request) -> str:
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


def parse_device_info(ua_string: str) -> dict:
    fallback = {
        'device_type': 'unknown',
        'device_name': 'Unknown Device',
        'browser': 'Unknown',
        'os': 'Unknown',
    }
    if not ua_string:
        return fallback
    try:
        from user_agents import parse as ua_parse
        ua = ua_parse(ua_string)

        if ua.is_mobile:
            device_type = 'mobile'
        elif ua.is_tablet:
            device_type = 'tablet'
        elif ua.is_pc:
            device_type = 'desktop'
        else:
            device_type = 'unknown'

        browser_fam = ua.browser.family or 'Unknown'
        browser_ver = (ua.browser.version_string or '').split('.')[0]
        browser = f"{browser_fam} {browser_ver}".strip()

        os_fam = ua.os.family or 'Unknown'
        os_ver = (ua.os.version_string or '').split('.')[0]
        os_name = f"{os_fam} {os_ver}".strip()

        # For mobile/tablet show the hardware model when available
        device_brand = (ua.device.brand or '').strip()
        device_model = (ua.device.model or '').strip()
        device_hw = f"{device_brand} {device_model}".strip()
        generic = device_hw.lower() in ('', 'other', 'generic smartphone', 'generic tablet', 'generic')

        if device_type in ('mobile', 'tablet') and device_hw and not generic:
            device_name = f"{device_hw} · {browser}"
        else:
            device_name = f"{browser} on {os_name}"

        return {
            'device_type': device_type,
            'device_name': device_name[:200],
            'browser': browser[:100],
            'os': os_name[:100],
        }
    except ImportError:
        logger.warning("user-agents not installed. Run: pip install user-agents")
        return fallback
    except Exception as exc:
        logger.warning("parse_device_info failed: %s", exc)
        return fallback


def register_session(user, jti: str, request, is_vendor: bool = False) -> None:
    """Create or refresh a UserSession row keyed on the refresh token's jti."""
    from .models import UserSession
    ip = get_client_ip(request)
    ua_string = request.META.get('HTTP_USER_AGENT', '')[:500]
    device_info = parse_device_info(ua_string)
    try:
        UserSession.objects.update_or_create(
            session_key=jti,
            defaults={
                'user': user,
                'ip_address': ip or None,
                'is_vendor_session': is_vendor,
                'last_activity': timezone.now(),
                **device_info,
            },
        )
    except Exception as exc:
        logger.error("register_session: failed for user %s: %s", user.pk, exc)


def touch_session(jti: str) -> None:
    """Update last_activity for an existing session without a full object load."""
    from .models import UserSession
    try:
        UserSession.objects.filter(session_key=jti).update(last_activity=timezone.now())
    except Exception:
        pass


# ── Revocation ────────────────────────────────────────────────────────────────
# Deleting a UserSession row alone does not sign a device out: its refresh
# token still verifies and its access token lives until expiry. Revoking a
# session therefore also
#   1. blacklists the refresh token, so it can never be renewed, and
#   2. marks the session id in the cache for the access-token lifetime, so
#      CustomJWTAuthentication rejects the device's next request.
# Seller tokens carry the session id in a `sid` claim (the refresh token's jti,
# copied onto every access token minted from it).

REVOKED_SID_PREFIX = 'revoked_sid:'


def _access_lifetime_seconds() -> int:
    from django.conf import settings
    lifetime = settings.SIMPLE_JWT.get('ACCESS_TOKEN_LIFETIME')
    seconds = int(lifetime.total_seconds()) if lifetime else 3600
    vendor = int(getattr(settings, 'VENDOR_AUTH_ACCESS_MAX_AGE', 0) or 0)
    return max(seconds, vendor) + 60


def is_session_revoked(sid: str) -> bool:
    from django.core.cache import cache
    if not sid:
        return False
    try:
        return bool(cache.get(REVOKED_SID_PREFIX + sid))
    except Exception:
        return False


def revoke_sessions(sessions) -> int:
    """Sign out every session in the queryset (or iterable). Returns the count."""
    from django.core.cache import cache
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken, OutstandingToken,
    )
    sessions = list(sessions)
    if not sessions:
        return 0
    jtis = [s.session_key for s in sessions]

    for outstanding in OutstandingToken.objects.filter(jti__in=jtis):
        BlacklistedToken.objects.get_or_create(token=outstanding)
    try:
        cache.set_many({REVOKED_SID_PREFIX + j: 1 for j in jtis}, _access_lifetime_seconds())
    except Exception as exc:
        logger.warning("revoke_sessions: cache mark failed: %s", exc)

    from .models import UserSession
    UserSession.objects.filter(pk__in=[s.pk for s in sessions]).delete()
    return len(sessions)


def record_security_event(user, event: str, request=None, *, is_vendor=True,
                          new_device=False, **metadata):
    """Append to the person's security timeline. Returns the event, or None; never raises."""
    from .models import SecurityEvent
    try:
        info = {}
        ip = None
        if request is not None:
            ip = get_client_ip(request) or None
            info = parse_device_info(request.META.get('HTTP_USER_AGENT', '')[:500])
        return SecurityEvent.objects.create(
            user=user,
            event=event,
            is_vendor=is_vendor,
            ip_address=ip,
            device_name=info.get('device_name', ''),
            device_type=info.get('device_type', ''),
            new_device=new_device,
            metadata=metadata,
        )
    except Exception as exc:
        logger.error("record_security_event: %s for user %s failed: %s", event, getattr(user, 'pk', None), exc)
        return None

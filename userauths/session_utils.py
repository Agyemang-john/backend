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

        return {
            'device_type': device_type,
            'device_name': f"{browser} on {os_name}"[:200],
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

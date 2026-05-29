import requests
from django.conf import settings

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify_turnstile(token: str, remote_ip: str = None) -> bool:
    """Return True if the Turnstile token is valid. Always returns True in DEBUG mode."""
    if getattr(settings, 'DEBUG', False):
        return True

    if not token:
        return False

    payload = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        resp = requests.post(_VERIFY_URL, data=payload, timeout=5)
        return resp.json().get("success", False)
    except Exception:
        return False

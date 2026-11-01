"""
Sending push notifications.

Every in-app Notification is also pushed to the recipient's devices for the
app whose bell it belongs in (notification.models.in_app): a seller's order
alert reaches the seller dashboard PWA, a shopper's delivery update reaches
the shop. Dead subscriptions (404/410, FCM UNREGISTERED) are deleted; a
per-user hourly cap keeps a burst (e.g. a bulk import) from spamming phones.

Configuration (settings / env):
  WEBPUSH_VAPID_PUBLIC_KEY, WEBPUSH_VAPID_PRIVATE_KEY, WEBPUSH_SUBJECT (mailto:)
  FCM_PROJECT_ID, FCM_SERVICE_ACCOUNT_FILE      (optional, mobile app)
Generate VAPID keys once:  python manage.py generate_vapid_keys
"""
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import PushSubscription

logger = logging.getLogger(__name__)

MAX_FAILURES = 5
SITE = {PushSubscription.SHOP: lambda: settings.SITE_URL, PushSubscription.SELLER: lambda: settings.SELLER_SITE_URL}


def webpush_enabled():
    return bool(getattr(settings, 'WEBPUSH_VAPID_PRIVATE_KEY', '') and getattr(settings, 'WEBPUSH_VAPID_PUBLIC_KEY', ''))


def fcm_enabled():
    return bool(getattr(settings, 'FCM_PROJECT_ID', '') and getattr(settings, 'FCM_SERVICE_ACCOUNT_FILE', ''))


def _under_cap(user_id):
    key = f"push:count:{user_id}:{timezone.now():%Y%m%d%H}"
    if cache.add(key, 1, 3700):
        return True
    try:
        return cache.incr(key) <= getattr(settings, 'PUSH_HOURLY_CAP', 20)
    except ValueError:
        return True


def payload_for(notification, app):
    data = notification.data or {}
    url = data.get('url') or '/'
    if url.startswith('/'):
        url = SITE[app]().rstrip('/') + url
    return {
        'title': data.get('title') or 'Negromart',
        'body': (data.get('message') or '')[:240],
        'url': url,
        'tag': f"n{notification.pk}",
        'notification_id': notification.pk,
    }


def deliver(notification):
    """Push one Notification to the recipient's devices. Returns how many were sent."""
    from notification.models import in_app

    subs = list(PushSubscription.objects.filter(user_id=notification.recipient_id))
    subs = [s for s in subs if in_app('seller' if s.app == PushSubscription.SELLER else 'shopper',
                                       notification.verb, notification.data)]
    if not subs or not _under_cap(notification.recipient_id):
        return 0
    sent = 0
    for sub in subs:
        payload = payload_for(notification, sub.app)
        ok = _send_webpush(sub, payload) if sub.kind == PushSubscription.WEBPUSH else _send_fcm(sub, payload)
        if ok:
            sent += 1
    return sent


def _gone(sub):
    sub.delete()


def _failed(sub):
    sub.failures += 1
    if sub.failures >= MAX_FAILURES:
        sub.delete()
    else:
        sub.save(update_fields=['failures'])


def _ok(sub):
    PushSubscription.objects.filter(pk=sub.pk).update(last_success_at=timezone.now(), failures=0)


def _send_webpush(sub, payload):
    if not webpush_enabled():
        return False
    from pywebpush import WebPushException, webpush
    try:
        webpush(
            subscription_info={'endpoint': sub.endpoint, 'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}},
            data=json.dumps(payload),
            vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
            vapid_claims={'sub': getattr(settings, 'WEBPUSH_SUBJECT', 'mailto:support@negromart.com')},
            ttl=24 * 3600,
            timeout=10,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in (404, 410):
            _gone(sub)
        else:
            logger.warning("web push to sub %s failed: %s", sub.pk, status or exc)
            _failed(sub)
        return False
    except Exception:  # noqa: BLE001 - network
        logger.exception("web push to sub %s failed", sub.pk)
        _failed(sub)
        return False
    _ok(sub)
    return True


_fcm_creds = None


def _fcm_token():
    global _fcm_creds
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    if _fcm_creds is None:
        _fcm_creds = service_account.Credentials.from_service_account_file(
            settings.FCM_SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/firebase.messaging'])
    if not _fcm_creds.valid:
        _fcm_creds.refresh(Request())
    return _fcm_creds.token


def _send_fcm(sub, payload):
    if not fcm_enabled():
        return False
    import requests
    try:
        res = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{settings.FCM_PROJECT_ID}/messages:send",
            headers={'Authorization': f"Bearer {_fcm_token()}"},
            json={'message': {'token': sub.endpoint,
                              'notification': {'title': payload['title'], 'body': payload['body']},
                              'data': {'url': payload['url'], 'notification_id': str(payload['notification_id'])}}},
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        logger.exception("FCM push to sub %s failed", sub.pk)
        _failed(sub)
        return False
    if res.status_code == 404 or 'UNREGISTERED' in res.text:
        _gone(sub)
        return False
    if res.status_code >= 400:
        logger.warning("FCM push to sub %s rejected: %s %s", sub.pk, res.status_code, res.text[:200])
        _failed(sub)
        return False
    _ok(sub)
    return True

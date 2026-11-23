"""
tools/webhooks.py
Tell a store's own systems about its orders as they happen.

  events     order.created (a new order with the store's items) and
             order.updated (the order's status changed). Each store only gets
             its own lines of an order.
  delivery   POST JSON from a Celery task, signed:
               X-Negromart-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>" with the secret>
             2xx within TIMEOUT counts as delivered. Otherwise retried after
             RETRY_DELAYS, then marked failed. After MAX_FAILURES failed
             deliveries in a row the webhook is switched off (the seller can
             switch it back on).
  safety     https only, port 443, no redirects, and every address the host
             resolves to must be public (checked again on the connected socket),
             so a webhook can't reach the platform's own network.
  retention  deliveries are kept KEEP_DAYS days (the log in Seller Centre).
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from datetime import timedelta
from urllib.parse import urlparse

import requests
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Webhook, WebhookDelivery

logger = logging.getLogger(__name__)

EVENTS = {'order.created': 'A new order with your items', 'order.updated': 'An order changed status'}
MAX_WEBHOOKS = 3
TIMEOUT = (4, 10)
RETRY_DELAYS = [60, 5 * 60, 30 * 60, 2 * 3600, 6 * 3600]      # then failed (6 tries in about 9 hours)
MAX_FAILURES = 20
KEEP_DAYS = 14
USER_AGENT = 'Negromart-Webhooks/1.0'


class WebhookError(ValueError):
    """Message is safe to show to the seller."""


# ── Setup ────────────────────────────────────────────────────────────────────

def check_url(url):
    """Raises WebhookError unless the URL is https on 443 and every address it resolves to is public."""
    parts = urlparse(str(url or '').strip())
    if parts.scheme != 'https' or not parts.hostname:
        raise WebhookError('Use an https:// address.')
    if parts.port not in (None, 443):
        raise WebhookError('Use the normal https port (443).')
    if parts.username or parts.password:
        raise WebhookError('Leave the user name and password out of the address.')
    try:
        infos = socket.getaddrinfo(parts.hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise WebhookError('That website could not be found.')
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise WebhookError('That address points to a private network.')


def _clean_events(events):
    events = [e for e in (events or []) if e in EVENTS]
    if not events:
        raise WebhookError('Choose at least one event.')
    return sorted(set(events))


def create(vendor, url, events):
    url = str(url or '').strip()[:300]
    events = _clean_events(events)
    check_url(url)
    if Webhook.objects.filter(vendor=vendor).count() >= MAX_WEBHOOKS:
        raise WebhookError(f'Up to {MAX_WEBHOOKS} webhooks. Delete one first.')
    return Webhook.objects.create(vendor=vendor, url=url, events=events, secret=f'whsec_{secrets.token_hex(24)}')


def update(webhook, url=None, events=None, is_active=None, roll_secret=False):
    if url is not None and url != webhook.url:
        url = str(url).strip()[:300]
        check_url(url)
        webhook.url = url
    if events is not None:
        webhook.events = _clean_events(events)
    if is_active is not None:
        webhook.is_active = bool(is_active)
        if webhook.is_active:
            webhook.consecutive_failures, webhook.disabled_reason = 0, ''
    if roll_secret:
        webhook.secret = f'whsec_{secrets.token_hex(24)}'
    webhook.save()
    return webhook


# ── Events ───────────────────────────────────────────────────────────────────

def order_event(order_id, event):
    """Queue `event` for every store in the order that has a webhook for it (call after commit)."""
    from order.models import Order, OrderProduct
    from payments.entitlements import plan_for_vendor
    from tools import registry
    from tools.labels import _ship_to, number
    from tools.seller_api import order_json

    hooks = list(Webhook.objects.filter(vendor__order__pk=order_id, is_active=True, events__contains=[event])
                 .select_related('vendor'))
    hooks = [h for h in hooks if registry.included(plan_for_vendor(h.vendor), 'api')]
    if not hooks:
        return 0
    order = Order.objects.select_related('address', 'address__pickup_point', 'user').get(pk=order_id)
    lines = {}
    for line in OrderProduct.objects.filter(order=order).select_related('product', 'variant').order_by('pk'):
        if line.product is not None:
            lines.setdefault(line.product.vendor_id, []).append(line)
    ship_to, order_number = _ship_to(order), number(order)
    deliveries = []
    for hook in hooks:
        own = lines.get(hook.vendor_id)
        if not own:
            continue
        deliveries.append(WebhookDelivery(webhook=hook, event=event, payload={
            'event': event, 'created_at': timezone.now().isoformat(),
            'data': order_json(order, own, ship_to, order_number)}))
    WebhookDelivery.objects.bulk_create(deliveries)
    from tools.tasks import deliver_webhook
    for d in deliveries:
        transaction.on_commit(lambda pk=d.pk: deliver_webhook.delay(pk))
    return len(deliveries)


def order_event_later(order_id, event):
    def run():
        try:
            order_event(order_id, event)
        except Exception:           # the order itself is fine; never fail checkout over a webhook
            logger.exception('Webhook %s for order %s could not be queued', event, order_id)
    transaction.on_commit(run)


# ── Sending ──────────────────────────────────────────────────────────────────

def sign(secret, body, timestamp=None):
    timestamp = int(timestamp or time.time())
    digest = hmac.new(secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()
    return f't={timestamp},v1={digest}'


def _post(webhook, delivery_id, event, payload):
    """One attempt. Returns (ok, response_code or None, error)."""
    body = json.dumps(payload, separators=(',', ':'), default=str).encode()
    try:
        check_url(webhook.url)
        response = requests.post(webhook.url, data=body, timeout=TIMEOUT, allow_redirects=False, stream=True, headers={
            'Content-Type': 'application/json', 'User-Agent': USER_AGENT, 'X-Negromart-Event': event,
            'X-Negromart-Delivery': str(delivery_id), 'X-Negromart-Signature': sign(webhook.secret, body)})
    except WebhookError as exc:
        return False, None, str(exc)
    except requests.RequestException as exc:
        return False, None, type(exc).__name__.replace('Error', ' error').strip()[:200]
    with response:
        try:
            peer = response.raw.connection.sock.getpeername()[0]
            if not ipaddress.ip_address(peer).is_global:
                return False, None, 'That address points to a private network.'
        except Exception:            # pragma: no cover - transport without a socket (tests)
            pass
        ok = 200 <= response.status_code < 300
        return ok, response.status_code, '' if ok else f'Answered {response.status_code}'


def deliver(delivery_id):
    """Try one delivery. Returns the delay in seconds before the next try, or None when done."""
    delivery = WebhookDelivery.objects.select_related('webhook').filter(pk=delivery_id, status=WebhookDelivery.PENDING).first()
    if delivery is None:
        return None
    hook = delivery.webhook
    if not hook.is_active:
        WebhookDelivery.objects.filter(pk=delivery.pk).update(status=WebhookDelivery.FAILED, error='Webhook switched off')
        return None
    ok, code, error = _post(hook, delivery.pk, delivery.event, delivery.payload)
    now = timezone.now()
    attempts = delivery.attempts + 1
    if ok:
        WebhookDelivery.objects.filter(pk=delivery.pk).update(status=WebhookDelivery.SENT, attempts=attempts,
                                                              response_code=code, error='', delivered_at=now)
        Webhook.objects.filter(pk=hook.pk).update(consecutive_failures=0, last_delivery_at=now)
        return None
    if attempts <= len(RETRY_DELAYS):
        WebhookDelivery.objects.filter(pk=delivery.pk).update(attempts=attempts, response_code=code, error=error)
        return RETRY_DELAYS[attempts - 1]
    WebhookDelivery.objects.filter(pk=delivery.pk).update(status=WebhookDelivery.FAILED, attempts=attempts,
                                                          response_code=code, error=error)
    Webhook.objects.filter(pk=hook.pk).update(consecutive_failures=F('consecutive_failures') + 1)
    Webhook.objects.filter(pk=hook.pk, consecutive_failures__gte=MAX_FAILURES, is_active=True).update(
        is_active=False, disabled_reason=f'Switched off after {MAX_FAILURES} failed deliveries in a row.')
    return None


def send_test(webhook):
    """A ping sent right away so the seller can check their endpoint. Returns (ok, message)."""
    payload = {'event': 'ping', 'created_at': timezone.now().isoformat(), 'data': {'store': webhook.vendor.name}}
    ok, code, error = _post(webhook, 'test', 'ping', payload)
    return ok, f'Delivered (answered {code}).' if ok else (error or 'Not delivered.')


def purge_old():
    return WebhookDelivery.objects.filter(created_at__lt=timezone.now() - timedelta(days=KEEP_DAYS)).delete()[0]

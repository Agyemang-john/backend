"""
Error monitoring and request tracing.

- Request IDs: every request gets one (from X-Request-ID, else generated). It is
  echoed in the response header, stamped on every log line made while handling
  the request, and tagged on Sentry events, so a support ticket quoting the ID
  leads straight to the logs and the error.
- Sentry: enabled only when SENTRY_DSN is set. Personal data is never sent:
  no cookies, auth headers, request bodies or user emails; only the user id.
- /api/health/: database, cache and Celery-worker heartbeat, for uptime monitors.
"""
import contextvars
import logging
import re
import time
import uuid

_request_id = contextvars.ContextVar('request_id', default='-')
_VALID_ID = re.compile(r'^[A-Za-z0-9._-]{8,64}$')

SENSITIVE_KEYS = re.compile(r'(pass|secret|token|auth|cookie|card|cvv|pin|otp|session|signature|id_number|tin)', re.I)


def current_request_id():
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Adds %(request_id)s to every log record."""

    def filter(self, record):
        record.request_id = _request_id.get()
        return True


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get('X-Request-ID', '')
        rid = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        token = _request_id.set(rid)
        request.request_id = rid
        start = time.monotonic()
        try:
            try:
                import sentry_sdk
                sentry_sdk.get_current_scope().set_tag('request_id', rid)
            except Exception:  # noqa: BLE001 - sentry optional
                pass
            response = self.get_response(request)
            response['X-Request-ID'] = rid
            elapsed = (time.monotonic() - start) * 1000
            if elapsed > 2000:
                logging.getLogger('slow_requests').warning('%s %s took %.0f ms', request.method, request.path, elapsed)
            return response
        finally:
            _request_id.reset(token)


def _scrub(value, depth=0):
    if depth > 6:
        return '[…]'
    if isinstance(value, dict):
        return {k: ('[Filtered]' if SENSITIVE_KEYS.search(str(k)) else _scrub(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    return value


def before_send(event, hint):
    request = event.get('request') or {}
    request.pop('cookies', None)
    request.pop('data', None)            # bodies can hold addresses, phone numbers, KYC
    headers = request.get('headers') or {}
    for h in list(headers):
        if SENSITIVE_KEYS.search(h) or h.lower() in ('x-forwarded-for', 'x-real-ip'):
            headers[h] = '[Filtered]'
    if 'query_string' in request and request['query_string']:
        request['query_string'] = re.sub(r'((?:token|code|key|signature)=)[^&]*', r'\1[Filtered]', str(request['query_string']))
    user = event.get('user') or {}
    event['user'] = {'id': user.get('id')} if user.get('id') else {}
    if 'extra' in event:
        event['extra'] = _scrub(event['extra'])
    for frame_holder in (event.get('exception') or {}).get('values', []):
        for frame in (frame_holder.get('stacktrace') or {}).get('frames', []):
            if 'vars' in frame:
                frame['vars'] = _scrub(frame['vars'])
    return event


def init_sentry(dsn, environment, release, traces_sample_rate):
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.redis import RedisIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release or None,
        integrations=[DjangoIntegration(), CeleryIntegration(monitor_beat_tasks=True), RedisIntegration(),
                      LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
        before_send=before_send,
        max_request_body_size='never',
    )
    return True


# ── Health ────────────────────────────────────────────────────────────────────

HEARTBEAT_KEY = 'health:celery_heartbeat'


def health():
    """(ok, checks). A failing check makes the endpoint return 503."""
    from django.core.cache import cache
    from django.db import connection
    from django.utils import timezone

    checks = {}
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')
        checks['database'] = 'ok'
    except Exception as exc:  # noqa: BLE001
        checks['database'] = f'error: {type(exc).__name__}'
    try:
        cache.set('health:ping', 1, 10)
        checks['cache'] = 'ok' if cache.get('health:ping') == 1 else 'error: read-back failed'
    except Exception as exc:  # noqa: BLE001
        checks['cache'] = f'error: {type(exc).__name__}'
    beat = cache.get(HEARTBEAT_KEY)
    if beat is None:
        checks['celery'] = 'unknown: no heartbeat yet'
    else:
        age = (timezone.now().timestamp() - beat)
        checks['celery'] = 'ok' if age < 300 else f'stale: last heartbeat {int(age)}s ago'
    ok = all(v == 'ok' or v.startswith('unknown') for v in checks.values())
    return ok, checks

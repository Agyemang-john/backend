"""Who is acting (from the current request), and writing entries."""
import contextvars
import logging

logger = logging.getLogger(__name__)

_current_request = contextvars.ContextVar('audit_request', default=None)


class AuditContextMiddleware:
    """Remembers the request so signals can attribute changes. DRF authenticates
    inside the view and sets request.user on this same HttpRequest, so API users
    are attributed too, as long as the user is read at write time (below)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            _current_request.reset(token)


def _actor():
    request = _current_request.get()
    if request is None:
        return None, 'system', 'system', None, ''
    user = getattr(request, 'user', None)
    via = 'admin' if request.path.startswith('/dashboard/negromart/') else 'api'
    from core.client_ip import client_ip
    ip = client_ip(request) or None
    rid = getattr(request, 'request_id', '')
    if user is not None and getattr(user, 'is_authenticated', False):
        return user, f"{user.email} (#{user.pk})", via, ip, rid
    return None, 'anonymous', via, ip, rid


def write(action, instance, changes):
    from .models import AuditEntry
    try:
        actor, label, via, ip, rid = _actor()
        AuditEntry.objects.create(
            actor=actor, actor_label=label[:160], via=via, action=action,
            model=instance._meta.label, object_id=str(instance.pk)[:40], object_label=str(instance)[:200],
            changes=changes, ip=ip, request_id=rid[:64],
        )
    except Exception:  # noqa: BLE001 - auditing must never block the change itself
        logger.exception("audit entry failed for %s %s", instance._meta.label, instance.pk)


def history(instance, limit=50):
    from .models import AuditEntry
    return AuditEntry.objects.filter(model=instance._meta.label, object_id=str(instance.pk))[:limit]

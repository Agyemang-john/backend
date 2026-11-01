from celery import shared_task
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health_view(request):
    """GET /api/health/ — 200 when the database and cache work and Celery is alive, else 503."""
    from .observability import health
    ok, checks = health()
    return JsonResponse({'ok': ok, 'checks': checks}, status=200 if ok else 503)


@shared_task(ignore_result=True, expires=55)
def celery_heartbeat():
    """Every minute: proves a worker (and beat) are running. Read by /api/health/."""
    from django.core.cache import cache
    from django.utils import timezone

    from .observability import HEARTBEAT_KEY
    cache.set(HEARTBEAT_KEY, timezone.now().timestamp(), 600)

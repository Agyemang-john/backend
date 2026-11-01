from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task(ignore_result=True)
def prune_audit_log(days=730):
    """Weekly: entries older than two years go (keep longer if your accountant says so)."""
    from .models import AuditEntry
    return AuditEntry.objects.filter(created_at__lt=timezone.now() - timedelta(days=days)).delete()[0]

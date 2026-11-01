"""Every new in-app notification is also pushed to the recipient's devices (after commit, via Celery)."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from notification.models import Notification


@receiver(post_save, sender=Notification, dispatch_uid='push_on_notification')
def notification_created(sender, instance, created, **kwargs):
    if not created or kwargs.get('raw'):
        return
    from .services import fcm_enabled, webpush_enabled
    if not (webpush_enabled() or fcm_enabled()):
        return
    from .tasks import push_notification
    nid = instance.pk
    transaction.on_commit(lambda: push_notification.delay(nid))

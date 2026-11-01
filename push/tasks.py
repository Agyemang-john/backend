from celery import shared_task


@shared_task(ignore_result=True, soft_time_limit=60)
def push_notification(notification_id):
    from notification.models import Notification

    from .services import deliver
    n = Notification.objects.filter(pk=notification_id).first()
    return deliver(n) if n else 0

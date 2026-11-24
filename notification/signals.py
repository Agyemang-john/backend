# notification/signals.py
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Notification
from .tasks import send_ticket_reply_email

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Notification)
def broadcast_new_notification(sender, instance, created, **kwargs):
    """
    Fires once when a Notification row is INSERT-ed.
    Pushes a lightweight 'new_notification' event to the recipient's channel group.
    The consumers handle the rest (re-fetching list / updating count).
    """
    if created:
        from .utils import push_new_notification
        push_new_notification(instance)


from .models import TicketReply

@receiver(post_save, sender=TicketReply)
def queue_reply_email(sender, instance, created, **kwargs):
    if created and not instance.is_internal:
        send_ticket_reply_email.delay(instance.id)

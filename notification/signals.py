# notification/signals.py
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
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
    if not created:
        return

    event = {
        "type": "new_notification",
        "notification_id": instance.id,
        # Lets each socket skip notifications meant for the other app (models.in_app).
        "verb": instance.verb,
        "audience": (instance.data or {}).get("audience"),
    }
    group = f"user_{instance.recipient_id}"

    def push():
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        try:
            async_to_sync(channel_layer.group_send)(group, event)
        except Exception:
            logger.exception("notification push to %s failed", group)

    # After commit: the sockets recount from the database, and inside an open
    # transaction they would count without this row.
    transaction.on_commit(push)


from .models import TicketReply

@receiver(post_save, sender=TicketReply)
def queue_reply_email(sender, instance, created, **kwargs):
    if created and not instance.is_internal:
        send_ticket_reply_email.delay(instance.id)

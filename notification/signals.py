# notification/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import Notification
from .tasks import send_ticket_reply_email


@receiver(post_save, sender=Notification)
def broadcast_notification(sender, instance, created, **kwargs):
    if not created:
        return

    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    
    verb_display = str(instance.get_verb_display())

    payload = {
        "type": "new_notification",
        "notification": {
            "id": instance.id,
            "verb": instance.verb,
            "verb_display": verb_display,
            "data": instance.data or {},
            "created_at": instance.created_at.isoformat(),  # ← MATCH MODEL
            "is_read": False,                               # ← ADD THIS
            "unread": True,
        }
    }

    async_to_sync(channel_layer.group_send)(
        f"user_{instance.recipient.id}",
        payload
    )

    async_to_sync(channel_layer.group_send)(
        f"user_{instance.recipient.id}",
        {"type": "new_notification"}
    )


from .models import TicketReply

@receiver(post_save, sender=TicketReply)
def queue_reply_email(sender, instance, created, **kwargs):
    if created and not instance.is_internal:
        # Fire and forget — admin returns instantly
        send_ticket_reply_email.delay(instance.id)

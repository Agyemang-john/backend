# notifications/utils.py
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .serializers import NotificationSerializer
from .models import Notification

def send_notification(recipient, verb, target=None, actor=None, data=None):
    notification = Notification.objects.create(
        recipient=recipient,
        verb=verb,
        actor=actor,
        target=target,
        data=data or {},
    )

    # Send real-time via WebSocket
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f"user_{recipient.id}",
            {
                "type": "new_notification",
                "notification": NotificationSerializer(notification).data
            }
        )

    return notification
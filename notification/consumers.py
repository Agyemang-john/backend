# notification/consumers.py
import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Notification
from .serializers import NotificationSerializer

logger = logging.getLogger(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────

@database_sync_to_async
def get_notifications_for(user):
    qs = Notification.objects.filter(recipient=user).order_by("-created_at")[:100]
    return list(NotificationSerializer(qs, many=True).data)


@database_sync_to_async
def get_unread_count_for(user):
    return Notification.objects.unread_count_for(user)


# ── Bell (count-only) consumer ────────────────────────────────────────────────

class NotificationCountConsumer(AsyncWebsocketConsumer):
    """
    Lightweight WebSocket for the notification bell.
    Only tracks the unread count — never sends full payloads.
    """

    async def connect(self):
        if self.scope["user"].is_anonymous:
            await self.close()
            return
        self.user = self.scope["user"]
        self.group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        # Send current count immediately on connect
        count = await get_unread_count_for(self.user)
        await self.send(text_data=json.dumps({"type": "unread_count", "count": count}))

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    # ── Group event handlers ──────────────────────────────────────────────────

    async def new_notification(self, event):
        """New notification created — update count and ring the bell."""
        count = await get_unread_count_for(self.user)
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": count,
            "trigger_toast": True,
        }))

    async def count_updated(self, event):
        """Count changed (e.g. user marked something read elsewhere)."""
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": event["count"],
        }))


# ── Full-list consumer ────────────────────────────────────────────────────────

class NotificationConsumer(AsyncWebsocketConsumer):
    """
    Full notification WebSocket for the notifications page / bell dropdown.
    Sends the complete list on connect and after every action.
    Supports: mark_read, mark_all_read, delete, view_detail.
    """

    async def connect(self):
        if self.scope["user"].is_anonymous:
            await self.close()
            return
        self.user = self.scope["user"]
        self.group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._send_refresh()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    # ── Client → Server ───────────────────────────────────────────────────────

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        action = data.get("action")

        if action == "mark_read":
            await self._mark_single_read(data.get("id"))

        elif action == "mark_all_read":
            await database_sync_to_async(
                Notification.objects.filter(recipient=self.user, is_read=False).update
            )(is_read=True)

        elif action == "delete":
            await database_sync_to_async(
                Notification.objects.filter(id=data.get("id"), recipient=self.user).delete
            )()

        elif action == "view_detail":
            await self._mark_single_read(data.get("id"))

        # Refresh this socket and sync the bell in every other tab
        count = await get_unread_count_for(self.user)
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "count_updated", "count": count},
        )
        await self._send_refresh()

    @database_sync_to_async
    def _mark_single_read(self, notif_id):
        if not notif_id:
            return
        Notification.objects.filter(id=notif_id, recipient=self.user, is_read=False).update(is_read=True)

    # ── Server → Client ───────────────────────────────────────────────────────

    async def new_notification(self, event):
        """
        A new Notification was just saved.
        Refresh the list so the new item appears immediately.
        Also push a count update to sync the bell.
        """
        count = await get_unread_count_for(self.user)
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "count_updated", "count": count},
        )
        await self._send_refresh()

    async def count_updated(self, event):
        """Bell sync — send lightweight count update to this socket too."""
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": event["count"],
        }))

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send_refresh(self):
        notifications = await get_notifications_for(self.user)
        count = await get_unread_count_for(self.user)
        await self.send(text_data=json.dumps({
            "type": "refresh_list",
            "notifications": notifications,
            "unread_count": count,
        }))

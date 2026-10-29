# notification/consumers.py
import json
import logging
from django.core.serializers.json import DjangoJSONEncoder
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Notification, app_of, in_app
from .serializers import NotificationSerializer

logger = logging.getLogger(__name__)


# ── DB helpers ────────────────────────────────────────────────────────────────

# Each socket shows the notifications of the app it was opened from (the
# seller dashboard or the shop: models.app_of / in_app), so counts are always
# worked out per socket rather than sent in group events.

@database_sync_to_async
def get_notifications_for(user):
    qs = Notification.objects.filter(recipient=user).for_app(app_of(user)).order_by("-created_at")[:100]
    return list(NotificationSerializer(qs, many=True).data)


@database_sync_to_async
def get_unread_count_for(user):
    return Notification.objects.unread_count_for(user, app_of(user))


def _for_this_app(user, event):
    return in_app(app_of(user), event.get("verb", ""), {"audience": event.get("audience")})


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
        await self.send(text_data=json.dumps({"type": "unread_count", "count": count}, cls=DjangoJSONEncoder))

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    # ── Group event handlers ──────────────────────────────────────────────────

    async def new_notification(self, event):
        """New notification created — update count and ring the bell (if it's this app's)."""
        if not _for_this_app(self.user, event):
            return
        count = await get_unread_count_for(self.user)
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": count,
            "trigger_toast": True,
        }, cls=DjangoJSONEncoder))

    async def count_updated(self, event):
        """Count changed (e.g. user marked something read elsewhere)."""
        count = await get_unread_count_for(self.user)
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": count,
        }, cls=DjangoJSONEncoder))


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
                Notification.objects.filter(recipient=self.user, is_read=False).for_app(app_of(self.user)).update
            )(is_read=True)

        elif action == "delete":
            await database_sync_to_async(
                Notification.objects.filter(id=data.get("id"), recipient=self.user).delete
            )()

        elif action == "view_detail":
            await self._mark_single_read(data.get("id"))

        elif action == "ping":
            await self.send(text_data='{"type": "pong"}')
            return

        else:
            return

        # Every open bell and list of this user (all tabs, both apps) recounts
        # and refreshes, this socket included.
        await self.channel_layer.group_send(self.group_name, {"type": "count_updated"})

    @database_sync_to_async
    def _mark_single_read(self, notif_id):
        if not notif_id:
            return
        Notification.objects.filter(id=notif_id, recipient=self.user, is_read=False).update(is_read=True)

    # ── Server → Client ───────────────────────────────────────────────────────

    async def new_notification(self, event):
        """
        A new Notification was just saved: refresh the list (it carries the
        count) and ring the bell, unless it belongs to the other app.
        """
        if not _for_this_app(self.user, event):
            return
        await self._send_refresh()
        count = await get_unread_count_for(self.user)
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": count,
            "trigger_toast": True,
        }, cls=DjangoJSONEncoder))

    async def count_updated(self, event):
        """Read state changed somewhere (another tab, the chat, REST): refresh list + count."""
        await self._send_refresh()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send_refresh(self):
        notifications = await get_notifications_for(self.user)
        count = await get_unread_count_for(self.user)
        await self.send(text_data=json.dumps({
            "type": "refresh_list",
            "notifications": notifications,
            "unread_count": count,
        }, cls=DjangoJSONEncoder))

# notification/consumers.py — FINAL, FLAWLESS, GOD-TIER VERSION
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Notification
from .serializers import NotificationSerializer

# LIGHTWEIGHT: BELL COUNTER ONLY
class NotificationCountConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        if self.scope["user"].is_anonymous:
            await self.close()
            return
        self.user = self.scope["user"]
        self.group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_unread_count()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def count_updated(self, event):
        # THIS IS THE REAL FIX — TRUST THE EVENT, DON'T RE-QUERY DB
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": event["count"]
        }))

    async def send_unread_count(self):
        count = await database_sync_to_async(Notification.objects.unread_count_for)(self.user)
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": count
        }))
    
    async def new_notification(self, event):
        count = await database_sync_to_async(Notification.objects.unread_count_for)(self.user)
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": count,
            "trigger_toast": True
        }))

# FULL LIST + ACTIONS
@database_sync_to_async
def get_notifications(user):
    qs = Notification.objects.filter(recipient=user).order_by("-created_at")[:100]
    return list(NotificationSerializer(qs, many=True).data)

class NotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        if self.scope["user"].is_anonymous:
            await self.close()
            return
        self.user = self.scope["user"]
        self.group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Just send refresh_list on connect
        notifications = await get_notifications(self.user)
        count = await database_sync_to_async(Notification.objects.unread_count_for)(self.user)

        await self.send(text_data=json.dumps({
            "type": "refresh_list",
            "notifications": notifications,
            "unread_count": count
        }))

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)
        action = data.get("action")

        # === MARK ALL READ ===
        if action == "mark_all_read":
            await database_sync_to_async(
                Notification.objects.filter(recipient=self.user, is_read=False).update
            )(is_read=True)

        # === MARK SINGLE READ ===
        elif action == "mark_read":
            try:
                notif = await database_sync_to_async(Notification.objects.get)(
                    id=data["id"], recipient=self.user
                )
                notif.is_read = True
                await database_sync_to_async(notif.save)()
            except Notification.DoesNotExist:
                pass

        # === DELETE ===
        elif action == "delete":
            await database_sync_to_async(
                Notification.objects.filter(id=data["id"], recipient=self.user).delete
            )()

        # === VIEW DETAIL (AUTO MARK AS READ) ===
        elif action == "view_detail":
            try:
                notif = await database_sync_to_async(Notification.objects.get)(
                    id=data["id"], recipient=self.user
                )
                if not notif.is_read:
                    notif.is_read = True
                    await database_sync_to_async(notif.save)()
            except Notification.DoesNotExist:
                pass

        # === ONLY SEND UPDATE ONCE — THIS IS THE FIX ===
        # Get fresh count after any action
        count = await database_sync_to_async(Notification.objects.unread_count_for)(self.user)

        # UPDATE BELL IN ALL TABS (including count-only consumer)
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "count_updated", "count": count}
        )

        # UPDATE LIST IN CURRENT TAB
        notifications = await get_notifications(self.user)
        await self.send(text_data=json.dumps({
            "type": "refresh_list",
            "notifications": notifications,
            "unread_count": count
        }))

    async def count_updated(self, event):
        await self.send(text_data=json.dumps({
            "type": "unread_count",
            "count": event["count"]
        }))

    async def new_notification(self, event):
        # Keep your refresh_list logic
        notifications = await get_notifications(self.user)
        count = await database_sync_to_async(Notification.objects.unread_count_for)(self.user)
        await self.send(text_data=json.dumps({
            "type": "refresh_list",
            "notifications": notifications,
            "unread_count": count
        }))
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "count_updated", "count": count}
        )
"""
chat/consumers.py
One socket per open app tab:  ws/chat/?token=<access>&as=customer|vendor

Server → client (JSON):
    {"type": "message", "conversation": {...}, "message": {...}, "unread_chats": n}
    {"type": "read", "conversation_id", "side", "last_read_id", "unread_chats"?}
    {"type": "delivered", "conversation_id", "side", "last_delivered_id"}
    {"type": "typing", "conversation_id"}
    {"type": "pong"}
Client → server:
    {"action": "ping"}                              keeps presence fresh (every ~30 s)
    {"action": "typing", "conversation_id": 12}     forwarded to the other side

Messages are sent over REST (chat/views.py) so they get validation, throttling
and moderation in one place; the socket only carries live events.

Delivery receipts: a message pushed to one of the recipient's sockets is
"delivered" (✓✓ grey); so is everything that arrived while they had no socket
open, the moment one connects.
"""

import json
import time
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from rest_framework.exceptions import PermissionDenied

from . import realtime, services
from .models import Conversation, Side

TYPING_EVERY = 2.0  # seconds between forwarded typing events per conversation


@database_sync_to_async
def _vendor_id_for(user):
    from .services import resolve_vendor_side
    try:
        return resolve_vendor_side(user).pk
    except PermissionDenied:
        return None


@database_sync_to_async
def _counterpart(conversation_id, side, party_id):
    """(other side's party id) if this party is in the conversation, else None."""
    lookup = {'customer_id': party_id} if side == Side.CUSTOMER else {'vendor_id': party_id}
    row = Conversation.objects.filter(pk=conversation_id, **lookup).values('customer_id', 'vendor_id').first()
    if row is None:
        return None
    return row['vendor_id'] if side == Side.CUSTOMER else row['customer_id']


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get('user')
        if user is None or user.is_anonymous:
            await self.close(code=4401)
            return
        params = parse_qs(self.scope.get('query_string', b'').decode())
        self.side = Side.VENDOR if params.get('as', [''])[0] == Side.VENDOR else Side.CUSTOMER
        if self.side == Side.VENDOR:
            self.party_id = await _vendor_id_for(user)
            if self.party_id is None:
                await self.close(code=4403)
                return
        else:
            self.party_id = user.pk
        self.group_name = realtime.group_for(self.side, self.party_id)
        self.counterparts = {}   # conversation_id → other party id (verified once per socket)
        self.last_typing = {}
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await database_sync_to_async(realtime.mark_online)(self.side, self.party_id)
        await database_sync_to_async(services.deliver_pending)(self.side, self.party_id)

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
            await database_sync_to_async(realtime.mark_offline)(self.side, self.party_id)

    async def receive(self, text_data=None, bytes_data=None):
        try:
            data = json.loads(text_data or '{}')
        except json.JSONDecodeError:
            return
        action = data.get('action')
        if action == 'ping':
            await database_sync_to_async(realtime.mark_online)(self.side, self.party_id)
            await self.send(text_data='{"type": "pong"}')
        elif action == 'typing':
            await self._typing(data.get('conversation_id'))

    async def _typing(self, conversation_id):
        try:
            conversation_id = int(conversation_id)
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        if now - self.last_typing.get(conversation_id, 0) < TYPING_EVERY:
            return
        self.last_typing[conversation_id] = now
        if conversation_id not in self.counterparts:
            if len(self.counterparts) > 200:
                self.counterparts.clear()
            self.counterparts[conversation_id] = await _counterpart(conversation_id, self.side, self.party_id)
        other_id = self.counterparts[conversation_id]
        if other_id is not None:
            await self.channel_layer.group_send(
                realtime.group_for(Side.other(self.side), other_id),
                {'type': 'chat.typing', 'conversation_id': conversation_id},
            )

    # ── Group events → this socket ────────────────────────────────────────────

    async def chat_message(self, event):
        message = event['message']
        await self.send(text_data=json.dumps({
            'type': 'message', 'conversation': event['conversation'], 'message': message,
            'unread_chats': event.get('unread_chats'),
        }, default=str))
        if message.get('side') != self.side:
            # It reached the recipient's open tab: ✓ → ✓✓ for the sender.
            await database_sync_to_async(services.mark_delivered)(
                message['conversation_id'], self.side, self.party_id, message['id'])

    async def chat_read(self, event):
        payload = {
            'type': 'read', 'conversation_id': event['conversation_id'], 'side': event['side'],
            'last_read_id': event['last_read_id'],
        }
        if 'unread_chats' in event:
            payload['unread_chats'] = event['unread_chats']
        await self.send(text_data=json.dumps(payload))

    async def chat_delivered(self, event):
        await self.send(text_data=json.dumps({
            'type': 'delivered', 'conversation_id': event['conversation_id'], 'side': event['side'],
            'last_delivered_id': event['last_delivered_id'],
        }))

    async def chat_typing(self, event):
        await self.send(text_data=json.dumps({'type': 'typing', 'conversation_id': event['conversation_id']}))

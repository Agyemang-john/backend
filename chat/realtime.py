"""
chat/realtime.py
Live delivery for chat over Django Channels (Redis channel layer).

Groups — one per audience, so a person who both shops and sells gets each app's
events only on that app's socket:
    chat_c_<user_id>     the shopper's open tabs (storefront + customer dashboard)
    chat_v_<vendor_id>   every team member's seller-dashboard tabs

Events sent to a group (handled in chat/consumers.py):
    chat.message   {conversation (recipient's view), message}
    chat.read      {conversation_id, side, last_read_id}
    chat.typing    {conversation_id}

Presence lives in the cache (Redis): a socket refreshes chat:on:<side>:<id> every
PING_SECONDS; the key outlives a ping by a margin, so "online" means "had a chat
socket open in the last ~minute". No per-message DB work, no counters to drift.

When the recipient side is offline the message also raises a bell notification
(notification.utils.send_notification), at most once per conversation per
NOTIFY_COOLDOWN, so a burst of messages is one alert.
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('chat')

PING_SECONDS = 30
ONLINE_TTL = 75
LAST_SEEN_TTL = 60 * 60 * 24 * 30
NOTIFY_COOLDOWN = 60 * 10


def group_for(side, party_id):
    return f"chat_{'v' if side == 'vendor' else 'c'}_{party_id}"


# ── Presence ──────────────────────────────────────────────────────────────────

def _online_key(side, party_id):
    return f"chat:on:{side}:{party_id}"


def _seen_key(side, party_id):
    return f"chat:seen:{side}:{party_id}"


def mark_online(side, party_id):
    cache.set(_online_key(side, party_id), 1, ONLINE_TTL)
    cache.set(_seen_key(side, party_id), timezone.now().isoformat(), LAST_SEEN_TTL)


def presence(side, party_id):
    """(online, last_seen ISO string or None)."""
    values = cache.get_many([_online_key(side, party_id), _seen_key(side, party_id)])
    return bool(values.get(_online_key(side, party_id))), values.get(_seen_key(side, party_id))


def presence_many(side, party_by_conversation):
    """{conversation_id: (online, last_seen)} in one cache round trip."""
    keys = []
    for party_id in set(party_by_conversation.values()):
        keys += [_online_key(side, party_id), _seen_key(side, party_id)]
    values = cache.get_many(keys) if keys else {}
    return {
        conv_id: (bool(values.get(_online_key(side, pid))), values.get(_seen_key(side, pid)))
        for conv_id, pid in party_by_conversation.items()
    }


def is_online(side, party_id):
    return bool(cache.get(_online_key(side, party_id)))


# ── Push ──────────────────────────────────────────────────────────────────────

def _send(group, event):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, event)
    except Exception:  # a push failure must never fail the request; clients resync on reconnect
        logger.exception("chat push to %s failed", group)


def message_created(conversation_id, message_id):
    from .models import Conversation, Message, Side
    from .serializers import ConversationSerializer, MessageSerializer

    conversation = (Conversation.objects.select_related('customer', 'vendor__about', 'product')
                    .filter(pk=conversation_id).first())
    message = Message.objects.select_related('sender', 'product').filter(pk=message_id).first()
    if conversation is None or message is None:
        return
    message_data = MessageSerializer(message).data
    for side, party_id in ((Side.CUSTOMER, conversation.customer_id), (Side.VENDOR, conversation.vendor_id)):
        _send(group_for(side, party_id), {
            'type': 'chat.message',
            'conversation': ConversationSerializer(conversation, context={'side': side}).data,
            'message': message_data,
        })

    recipient = Side.other(message.sender_side)
    recipient_id = conversation.customer_id if recipient == Side.CUSTOMER else conversation.vendor_id
    if not is_online(recipient, recipient_id):
        notify_offline(conversation, message, recipient)


def read_receipt(conversation, side, last_read_id):
    from .models import Side
    other = Side.other(side)
    other_id = conversation.customer_id if other == Side.CUSTOMER else conversation.vendor_id
    event = {'type': 'chat.read', 'conversation_id': conversation.pk, 'side': side, 'last_read_id': last_read_id}
    _send(group_for(other, other_id), event)
    # The reader's other tabs/devices clear their unread badge too.
    own_id = conversation.customer_id if side == Side.CUSTOMER else conversation.vendor_id
    _send(group_for(side, own_id), event)


def typing(conversation_id, to_side, to_party_id):
    _send(group_for(to_side, to_party_id), {'type': 'chat.typing', 'conversation_id': conversation_id})


# ── Offline alerts ────────────────────────────────────────────────────────────

def notify_offline(conversation, message, recipient_side):
    from notification.utils import send_notification

    from .models import Side
    from .serializers import customer_display

    if not cache.add(f"chat:notified:{conversation.pk}:{recipient_side}", 1, NOTIFY_COOLDOWN):
        return
    preview = message.body if len(message.body) <= 120 else message.body[:117] + '…'
    try:
        if recipient_side == Side.CUSTOMER:
            send_notification(conversation.customer, 'message', target=conversation, data={
                'message': f"{conversation.vendor.name}: {preview}",
                'url': f"/dashboard/messages?c={conversation.pk}",
                'conversation_id': conversation.pk,
                'audience': 'customer',
            })
            return
        name, _ = customer_display(conversation.customer)
        for user in vendor_recipients(conversation.vendor_id):
            send_notification(user, 'message', target=conversation, data={
                'message': f"{name}: {preview}",
                'url': f"/messages?c={conversation.pk}",
                'conversation_id': conversation.pk,
                'audience': 'vendor',
            })
    except Exception:
        logger.exception("chat offline notification failed for conversation %s", conversation.pk)


def vendor_recipients(vendor_id):
    """Team members whose role includes customer messages."""
    from vendor.access import Capability, capabilities_for_role
    from vendor.models import VendorMember

    members = VendorMember.objects.filter(vendor_id=vendor_id, is_active=True).select_related('user')[:20]
    return [m.user for m in members if Capability.MANAGE_MESSAGES in capabilities_for_role(m.role)]

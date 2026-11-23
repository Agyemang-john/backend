"""
tools/chat_tools.py
Shopper chat helpers for stores:

  saved replies   answers the team inserts with one tap (up to MAX_REPLIES),
                  checked by the same moderation as chat messages.
  away message    posted automatically when a shopper writes while the store is
                  away: always (holiday) or outside its opening hours. At most
                  once per conversation every AUTO_REPLY_GAP, marked automatic,
                  and it doesn't mark the chat read for the store.
  response stats  over the last 30 days: median time to a first reply, share
                  answered within an hour, chats waiting for a reply now.
"""
import logging
from datetime import timedelta
from statistics import median

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import AwaySetting, SavedReply

logger = logging.getLogger(__name__)

MAX_REPLIES = 50
AUTO_REPLY_GAP = timedelta(hours=12)
STATS_DAYS = 30
STATS_MESSAGE_LIMIT = 20000


class ChatToolError(ValueError):
    """Message is safe to show to the seller."""


def _moderate(text):
    from chat import moderation
    found = moderation.check(text)
    if found:
        raise ChatToolError(moderation.explain(found))


def save_reply(vendor, title, body, reply=None):
    title = ' '.join(str(title or '').split())[:40]
    body = str(body or '').strip()[:1000]
    if not title or len(body) < 2:
        raise ChatToolError('Give the reply a short name and some text.')
    _moderate(body)
    if reply is None:
        if SavedReply.objects.filter(vendor=vendor).count() >= MAX_REPLIES:
            raise ChatToolError(f'Up to {MAX_REPLIES} saved replies. Delete one you no longer use.')
        return SavedReply.objects.create(vendor=vendor, title=title, body=body)
    reply.title, reply.body = title, body
    reply.save(update_fields=['title', 'body', 'updated_at'])
    return reply


def count_use(vendor, reply_id):
    SavedReply.objects.filter(vendor=vendor, pk=reply_id).update(use_count=F('use_count') + 1)


def save_away(vendor, mode, message):
    if mode not in dict(AwaySetting.MODE_CHOICES):
        raise ChatToolError('Choose when the away message is sent.')
    message = str(message or '').strip()[:500]
    if mode != AwaySetting.OFF and len(message) < 10:
        raise ChatToolError('Write the away message (at least 10 characters).')
    if message:
        _moderate(message)
    setting, _ = AwaySetting.objects.update_or_create(vendor=vendor, defaults={'mode': mode, 'message': message})
    return setting


def is_away(vendor, setting):
    if setting.mode == AwaySetting.ALWAYS:
        return True
    if setting.mode == AwaySetting.OUTSIDE_HOURS:
        # Only when the store has set its hours; otherwise it would always be "away".
        return vendor.opening_hours.exists() and not vendor.is_open()
    return False


def auto_reply_later(conversation_id):
    transaction.on_commit(lambda: _safe_auto_reply(conversation_id))


def _safe_auto_reply(conversation_id):
    try:
        auto_reply(conversation_id)
    except Exception:          # the shopper's message is already saved; never fail it over this
        logger.exception('Away message failed for conversation %s', conversation_id)


def auto_reply(conversation_id, now=None):
    """Post the store's away message in this conversation if due. Returns the message or None."""
    from chat import realtime
    from chat.models import Conversation, Message, Side
    from payments.entitlements import plan_for_vendor
    from tools import registry

    now = now or timezone.now()
    conversation = Conversation.objects.select_related('vendor').filter(pk=conversation_id).first()
    if conversation is None:
        return None
    vendor = conversation.vendor
    setting = AwaySetting.objects.filter(vendor=vendor).first()
    if setting is None or setting.mode == AwaySetting.OFF or not setting.message:
        return None
    if not registry.included(plan_for_vendor(vendor), 'chat_tools') or not is_away(vendor, setting):
        return None
    if Message.objects.filter(conversation=conversation, is_automatic=True, created_at__gte=now - AUTO_REPLY_GAP).exists():
        return None
    with transaction.atomic():
        message = Message.objects.create(conversation=conversation, sender=None, sender_side=Side.VENDOR,
                                         body=setting.message, is_automatic=True)
        # The shopper has something new; the store still has the shopper's message to read.
        Conversation.objects.filter(pk=conversation.pk).update(
            last_message_at=message.created_at, last_message_preview=setting.message[:160],
            last_sender_side=Side.VENDOR, last_message_id=message.id, customer_unread=F('customer_unread') + 1)
    transaction.on_commit(lambda: realtime.message_created(conversation.pk, message.pk))
    return message


def response_stats(vendor, now=None):
    """First-response times over the last STATS_DAYS days (automatic replies don't count)."""
    from chat.models import Conversation, Message, Side

    now = now or timezone.now()
    since = now - timedelta(days=STATS_DAYS)
    rows = (Message.objects.filter(conversation__vendor=vendor, created_at__gte=since)
            .order_by('conversation_id', 'id')
            .values_list('conversation_id', 'sender_side', 'is_automatic', 'created_at')[:STATS_MESSAGE_LIMIT])
    waits, waiting_since, current = [], None, None
    for conversation_id, side, automatic, created in rows:
        if conversation_id != current:
            current, waiting_since = conversation_id, None
        if side == Side.CUSTOMER:
            waiting_since = waiting_since or created
        elif not automatic and waiting_since is not None:
            waits.append((created - waiting_since).total_seconds() / 60)
            waiting_since = None
    unanswered = Conversation.objects.filter(vendor=vendor, last_sender_side=Side.CUSTOMER).count()
    return {
        'days': STATS_DAYS,
        'answered': len(waits),
        'median_minutes': round(median(waits)) if waits else None,
        'within_hour_percent': round(100 * sum(1 for w in waits if w <= 60) / len(waits)) if waits else None,
        'waiting_now': unanswered,
    }

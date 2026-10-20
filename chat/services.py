"""
chat/services.py
The rules of shopper ↔ seller chat. Views and the websocket consumer call these;
nothing else writes Conversation / Message rows.

    resolve_vendor_side(user)          → the store this user answers chats for
    start_conversation(...)            → shopper opens (or reopens) a chat with a store
    send_message(...)                  → either side posts; moderation, counters, push
    mark_read(conversation, side)      → read receipts + unread counters
    muted_until(user)                  → repeat offenders can't send for a while
"""

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from vendor.access import Capability, get_membership, has_capability

from . import moderation, realtime
from .models import ChatViolation, Conversation, Message, Side

MAX_LENGTH = 1000
# Off-platform attempts allowed in VIOLATION_WINDOW before the sender is muted for MUTE_FOR.
MAX_VIOLATIONS = 3
VIOLATION_WINDOW = timedelta(hours=24)
MUTE_FOR = timedelta(hours=24)
# Spam guard: new stores a shopper may contact per day.
NEW_CONVERSATIONS_PER_DAY = 20
# A phone number typed in pieces is looked for across this many recent messages.
SPLIT_NUMBER_LOOKBACK = 4
SPLIT_NUMBER_WINDOW = timedelta(minutes=15)


class ChatError(Exception):
    def __init__(self, message, code='invalid', status=400, **extra):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


def retention_days():
    return int(getattr(settings, 'CHAT_RETENTION_DAYS', 30))


def store_accepts_messages(vendor):
    return bool(vendor.is_approved and not vendor.is_suspended)


# ── Who is talking ────────────────────────────────────────────────────────────

def resolve_vendor_side(user):
    """The store whose inbox this user may read and answer. Raises PermissionDenied."""
    membership = get_membership(user)
    if membership is None or not membership.vendor.is_approved:
        raise PermissionDenied("You must be a member of an approved store to answer customer messages.")
    if not has_capability(user, Capability.MANAGE_MESSAGES):
        raise PermissionDenied("Your team role does not include customer messages. Ask the store owner for access.")
    return membership.vendor


def conversations_for(user, side):
    if side == Side.VENDOR:
        return Conversation.objects.filter(vendor=resolve_vendor_side(user))
    return Conversation.objects.filter(customer=user)


# ── Mute ──────────────────────────────────────────────────────────────────────

def muted_until(user):
    recent = list(
        ChatViolation.objects.filter(user=user, created_at__gte=timezone.now() - VIOLATION_WINDOW)
        .order_by('-created_at').values_list('created_at', flat=True)[:MAX_VIOLATIONS]
    )
    if len(recent) < MAX_VIOLATIONS:
        return None
    until = recent[0] + MUTE_FOR
    return until if until > timezone.now() else None


def _muted_error(until):
    return ChatError(
        "You can't send messages right now because several of your messages tried to move the "
        "conversation off Negromart. You can message again later.",
        code='muted', status=429, muted_until=until.isoformat(),
    )


# ── Writing ───────────────────────────────────────────────────────────────────

def start_conversation(user, vendor, body, reason=Conversation.REASON_QUESTION, product=None, client_id=''):
    """Open a chat with a store (or continue the existing one) with a first message."""
    if not store_accepts_messages(vendor):
        raise ChatError("This store isn't accepting messages right now.", code='store_unavailable', status=409)
    membership = get_membership(user)
    if membership is not None and membership.vendor_id == vendor.pk:
        raise ChatError("You can't message your own store.", code='own_store')
    if reason not in dict(Conversation.REASON_CHOICES):
        reason = Conversation.REASON_QUESTION
    if product is not None and product.vendor_id != vendor.pk:
        product = None

    conversation = Conversation.objects.filter(customer=user, vendor=vendor).first()
    if conversation is None:
        since = timezone.now() - timedelta(days=1)
        if Conversation.objects.filter(customer=user, created_at__gte=since).count() >= NEW_CONVERSATIONS_PER_DAY:
            raise ChatError("You've contacted a lot of stores today. Please try again tomorrow.",
                            code='too_many_conversations', status=429)
        # Check the message before creating an empty conversation for it.
        _check_content(user, None, Side.CUSTOMER, body)
        try:
            with transaction.atomic():
                conversation = Conversation.objects.create(customer=user, vendor=vendor, reason=reason, product=product)
        except IntegrityError:  # opened in another tab at the same moment
            conversation = Conversation.objects.get(customer=user, vendor=vendor)
    else:
        changes = {'reason': reason}
        if product is not None:
            changes['product'] = product
        Conversation.objects.filter(pk=conversation.pk).update(**changes)
        for field, value in changes.items():
            setattr(conversation, field, value)

    message, _ = send_message(conversation, user, Side.CUSTOMER, body, product=product, client_id=client_id)
    conversation.refresh_from_db()
    return conversation, message


def send_message(conversation, user, side, body, product=None, client_id=''):
    """
    Post a message. Returns (message, created); a retry with the same client_id
    returns the stored message instead of posting twice.
    """
    client_id = (client_id or '')[:40]
    if client_id:
        existing = Message.objects.filter(conversation=conversation, client_id=client_id).first()
        if existing:
            return existing, False
    if side == Side.CUSTOMER and not store_accepts_messages(conversation.vendor):
        raise ChatError("This store isn't accepting messages right now.", code='store_unavailable', status=409)

    body = _check_content(user, conversation, side, body)

    other = Side.other(side)
    try:
        with transaction.atomic():
            message = Message.objects.create(conversation=conversation, sender=user, sender_side=side, body=body,
                                             product=product, client_id=client_id)
            Conversation.objects.filter(pk=conversation.pk).update(**{
                'last_message_at': message.created_at,
                'last_message_preview': body[:160],
                'last_sender_side': side,
                'last_message_id': message.id,
                f'{other}_unread': F(f'{other}_unread') + 1,
                # Replying means you've read the thread.
                f'{side}_unread': 0,
                f'{side}_last_read_id': message.id,
            })
    except IntegrityError:
        if client_id:  # the same retry raced us
            return Message.objects.get(conversation=conversation, client_id=client_id), False
        raise
    transaction.on_commit(lambda: realtime.message_created(conversation.pk, message.pk))
    return message, True


def _check_content(user, conversation, side, body):
    body = (body or '').strip()
    if not body:
        raise ChatError("Type a message first.", code='empty')
    if len(body) > MAX_LENGTH:
        raise ChatError(f"Messages can be up to {MAX_LENGTH} characters.", code='too_long')

    until = muted_until(user)
    if until:
        raise _muted_error(until)

    recent = []
    if conversation is not None:
        recent = list(
            Message.objects.filter(conversation=conversation, sender_side=side,
                                   created_at__gte=timezone.now() - SPLIT_NUMBER_WINDOW)
            .order_by('-id').values_list('body', flat=True)[:SPLIT_NUMBER_LOOKBACK]
        )[::-1]
    categories = moderation.check(body, recent=recent)
    if categories:
        ChatViolation.objects.create(user=user, conversation=conversation, side=side, categories=categories,
                                     excerpt=body[:1000])
        until = muted_until(user)
        if until:
            raise _muted_error(until)
        raise ChatError(moderation.explain(categories), code='off_platform', status=422, categories=categories)
    return body


# ── Reading ───────────────────────────────────────────────────────────────────

def mark_read(conversation, side):
    """Everything up to the latest message is read by `side`. Pushes a receipt to the other side."""
    if conversation.unread_for(side) == 0 and conversation.last_read_id_for(side) >= conversation.last_message_id:
        return False
    last_id = conversation.last_message_id
    Conversation.objects.filter(pk=conversation.pk).update(**{f'{side}_unread': 0, f'{side}_last_read_id': last_id})
    setattr(conversation, f'{side}_unread', 0)
    setattr(conversation, f'{side}_last_read_id', last_id)
    transaction.on_commit(lambda: realtime.read_receipt(conversation, side, last_id))
    return True

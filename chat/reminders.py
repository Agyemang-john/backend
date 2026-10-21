"""
chat/reminders.py
"Still waiting for your reply" nudges, in-app notification + SMS, run by
chat.tasks.send_chat_reminders every 30 minutes.

    Store   — a shopper's message has had no reply for CHAT_REMINDER_AFTER_HOURS
              (read or not: the shopper is waiting on an answer).
    Shopper — the store's reply has sat unread for CHAT_REMINDER_AFTER_HOURS.

One reminder per unanswered message (Conversation.reminded_message_id), claimed
with a conditional UPDATE so two workers can't both send it. Messages older than
the reminder window plus REMINDER_GRACE are skipped, so turning this on (or a
long worker outage) never floods people about stale chats.

SMS bodies never quote the message: just who, how long, and the link.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import Conversation, Side

logger = logging.getLogger('chat')

REMINDER_GRACE = timedelta(hours=48)
BATCH = 500


def reminder_after():
    return timedelta(hours=int(getattr(settings, 'CHAT_REMINDER_AFTER_HOURS', 24)))


def _hours_label():
    hours = int(reminder_after().total_seconds() // 3600)
    return '24 hrs' if hours == 24 else f'{hours} hrs'


def due_conversations(now=None):
    """(conversation, side to remind) pairs that are due, oldest first."""
    now = now or timezone.now()
    window = dict(last_message_at__lte=now - reminder_after(),
                  last_message_at__gte=now - reminder_after() - REMINDER_GRACE,
                  reminded_message_id__lt=F('last_message_id'))
    base = Conversation.objects.select_related('customer', 'vendor').order_by('last_message_at')
    stores = base.filter(last_sender_side=Side.CUSTOMER, **window)[:BATCH]
    shoppers = base.filter(last_sender_side=Side.VENDOR, customer_unread__gt=0, **window)[:BATCH]
    return [(c, Side.VENDOR) for c in stores] + [(c, Side.CUSTOMER) for c in shoppers]


def _claim(conversation):
    return Conversation.objects.filter(
        pk=conversation.pk, last_message_id=conversation.last_message_id,
        reminded_message_id__lt=conversation.last_message_id,
    ).update(reminded_message_id=conversation.last_message_id) == 1


def send_due_reminders(now=None):
    from .services import store_accepts_messages
    from .tasks import send_chat_sms

    sent = {'vendor': 0, 'customer': 0}
    for conversation, side in due_conversations(now):
        if side == Side.VENDOR and not store_accepts_messages(conversation.vendor):
            continue  # store can't use chat any more (plan ended, suspended)
        if not _claim(conversation):
            continue
        try:
            phone, sms = remind(conversation, side)
        except Exception:
            logger.exception("chat reminder failed for conversation %s", conversation.pk)
            continue
        if phone:
            send_chat_sms.delay(phone, sms)
        sent[side] += 1
    if any(sent.values()):
        logger.info("chat reminders sent: %s", sent)
    return sent


def remind(conversation, side):
    """Create the in-app notification(s); return (phone, sms text) for the SMS."""
    from notification.utils import send_notification

    from .realtime import vendor_recipients
    from .serializers import customer_display

    hours = _hours_label()
    if side == Side.VENDOR:
        shopper, _ = customer_display(conversation.customer)
        url = f"/messages?c={conversation.pk}"
        for user in vendor_recipients(conversation.vendor_id):
            send_notification(user, 'message', target=conversation, data={
                'title': 'A shopper is waiting for your reply',
                'message': f"{shopper} messaged your store {hours} ago and hasn't had a reply yet. "
                           f"Quick replies win more sales.",
                'url': url, 'conversation_id': conversation.pk, 'audience': 'vendor', 'reminder': True,
            })
        base = getattr(settings, 'SELLER_SITE_URL', 'https://seller.negromart.com').rstrip('/')
        sms = (f"Negromart: {shopper[:20]} messaged {conversation.vendor.name[:30]} {hours} ago and is waiting "
               f"for your reply. Reply here: {base}{url}")
        return (conversation.vendor.contact or '').strip(), sms

    store = conversation.vendor.name
    url = f"/dashboard/messages?c={conversation.pk}"
    send_notification(conversation.customer, 'message', target=conversation, data={
        'title': f"{store} replied to you",
        'message': f"{store} replied to your message {hours} ago. Have a look and reply when you're ready.",
        'url': url, 'conversation_id': conversation.pk, 'audience': 'customer', 'reminder': True,
    })
    base = (getattr(settings, 'SITE_URL', '') or 'https://www.negromart.com').rstrip('/')
    sms = f"Negromart: {store[:30]} replied to your message {hours} ago. Read and reply: {base}{url}"
    return (conversation.customer.phone or '').strip(), sms

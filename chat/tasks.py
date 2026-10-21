"""
chat/tasks.py
    purge_old_chats       nightly: chats are short-lived; messages older than
                          settings.CHAT_RETENTION_DAYS are deleted, and so are
                          conversations with nothing left in them. Batched so the
                          sweep never holds long locks on a busy table.
    send_chat_reminders   every 30 min: "waiting for your reply" (chat/reminders.py)
    send_chat_sms         one reminder SMS via Arkesel
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import ChatViolation, Conversation, Message
from .services import retention_days

logger = logging.getLogger('chat')

BATCH = 5000
# Off-platform attempts are kept longer than chats so staff can spot repeat offenders.
VIOLATION_RETENTION_DAYS = 90


def _delete_in_batches(qs):
    total = 0
    while True:
        ids = list(qs.values_list('pk', flat=True)[:BATCH])
        if not ids:
            return total
        total += qs.model.objects.filter(pk__in=ids).delete()[1].get(qs.model._meta.label, 0)


@shared_task
def purge_old_chats():
    now = timezone.now()
    cutoff = now - timedelta(days=retention_days())
    # Whole conversations first: their messages go with them (cascade).
    conversations = _delete_in_batches(Conversation.objects.filter(last_message_at__lt=cutoff))
    messages = _delete_in_batches(Message.objects.filter(created_at__lt=cutoff))
    violations = _delete_in_batches(
        ChatViolation.objects.filter(created_at__lt=now - timedelta(days=VIOLATION_RETENTION_DAYS)))
    logger.info("chat purge: %s conversations, %s messages, %s violations", conversations, messages, violations)
    return {'conversations': conversations, 'messages': messages, 'violations': violations}


@shared_task(ignore_result=True)
def send_chat_reminders():
    from .reminders import send_due_reminders
    return send_due_reminders()


@shared_task(bind=True, max_retries=3, ignore_result=True)
def send_chat_sms(self, phone, message):
    from userauths.tasks import _get_sms_client
    try:
        response = _get_sms_client().send_sms(sender=settings.ARKESEL_SENDER, message=message, recipients=[phone])
        if response.get('status') != 'success':
            raise Exception(f"Arkesel error: {response.get('message', response)}")
    except Exception as exc:
        logger.error("chat reminder SMS to %s failed: %s", phone[-4:], exc)
        raise self.retry(exc=exc, countdown=120)

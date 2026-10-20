"""
chat/tasks.py
Chats are short-lived: messages older than settings.CHAT_RETENTION_DAYS are
deleted every night, and so are conversations with nothing left in them.
Deletes run in small batches so the sweep never holds long locks on a busy table.
"""

import logging
from datetime import timedelta

from celery import shared_task
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

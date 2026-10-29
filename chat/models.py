"""
chat/models.py
Shopper ↔ seller messaging.

A Conversation is one thread between a shopper (a User) and a store (a Vendor),
whoever on the store's team answers. Messages are short-lived: chat/tasks.py
deletes them after settings.CHAT_RETENTION_DAYS.

Hot paths and how they stay cheap at scale:
  - chat lists        → (customer, -last_message_at) / (vendor, -last_message_at)
                        indexes; preview + unread counters are denormalised on
                        the Conversation so a list is one query, no joins on Message.
  - thread history    → keyset pagination on (conversation, -id).
  - read receipts     → each side's last_read_message_id; a message is "read"
                        when its id <= the other side's pointer (no per-message rows).
                        Delivery receipts work the same way with <side>_delivered_id.
"""

from django.conf import settings
from django.db import models

from vendor.models import Vendor


class Side:
    CUSTOMER = 'customer'
    VENDOR = 'vendor'
    CHOICES = [(CUSTOMER, 'Shopper'), (VENDOR, 'Store')]

    @staticmethod
    def other(side):
        return Side.VENDOR if side == Side.CUSTOMER else Side.CUSTOMER


class Conversation(models.Model):
    REASON_QUESTION = 'question'
    REASON_PRODUCT = 'product'
    REASON_ORDER = 'order'
    REASON_OTHER = 'other'
    REASON_CHOICES = [
        (REASON_QUESTION, 'Question or comment'),
        (REASON_PRODUCT, 'About a product'),
        (REASON_ORDER, 'About an order'),
        (REASON_OTHER, 'Something else'),
    ]

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                 related_name='chat_conversations')
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='chat_conversations')
    reason = models.CharField(max_length=12, choices=REASON_CHOICES, default=REASON_QUESTION)
    # The product the shopper most recently asked about (shown as context to the seller).
    product = models.ForeignKey('product.Product', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+')

    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_message_preview = models.CharField(max_length=160, blank=True, default='')
    last_sender_side = models.CharField(max_length=8, choices=Side.CHOICES, blank=True, default='')
    last_message_id = models.BigIntegerField(default=0)

    # Unread messages for each side, kept in step by chat/services.py with F() updates.
    customer_unread = models.PositiveIntegerField(default=0)
    vendor_unread = models.PositiveIntegerField(default=0)
    customer_last_read_id = models.BigIntegerField(default=0)
    vendor_last_read_id = models.BigIntegerField(default=0)
    # Delivery receipts (✓✓ grey): the newest message that reached one of the
    # side's open app tabs. Always >= that side's last_read_id.
    customer_delivered_id = models.BigIntegerField(default=0)
    vendor_delivered_id = models.BigIntegerField(default=0)
    # The last message a "still waiting for your reply" reminder was sent for
    # (chat/tasks.py send_chat_reminders): one reminder per unanswered message.
    reminded_message_id = models.BigIntegerField(default=0)

    class Meta:
        ordering = ['-last_message_at']
        constraints = [
            models.UniqueConstraint(fields=['customer', 'vendor'], name='uniq_chat_customer_vendor'),
        ]
        indexes = [
            models.Index(fields=['customer', '-last_message_at'], name='chat_conv_customer_idx'),
            models.Index(fields=['vendor', '-last_message_at'], name='chat_conv_vendor_idx'),
            models.Index(fields=['last_message_at'], name='chat_conv_last_msg_idx'),  # retention sweep
        ]

    def __str__(self):
        return f"Chat #{self.pk}: {self.customer_id} ↔ {self.vendor}"

    def unread_for(self, side):
        return self.customer_unread if side == Side.CUSTOMER else self.vendor_unread

    def last_read_id_for(self, side):
        return self.customer_last_read_id if side == Side.CUSTOMER else self.vendor_last_read_id

    def delivered_id_for(self, side):
        # Read implies delivered (rows from before delivery receipts only have the read pointer).
        delivered = self.customer_delivered_id if side == Side.CUSTOMER else self.vendor_delivered_id
        return max(delivered, self.last_read_id_for(side))


class Message(models.Model):
    id = models.BigAutoField(primary_key=True)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='+')
    sender_side = models.CharField(max_length=8, choices=Side.CHOICES)
    body = models.TextField(max_length=1000)
    # Optional product card ("I'm asking about this item").
    product = models.ForeignKey('product.Product', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+')
    # Generated by the sender's browser; makes retries idempotent and lets the
    # UI swap its optimistic bubble for the stored one.
    client_id = models.CharField(max_length=40, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['conversation', '-id'], name='chat_msg_thread_idx'),
            models.Index(fields=['created_at'], name='chat_msg_created_idx'),  # retention sweep
        ]
        constraints = [
            models.UniqueConstraint(fields=['conversation', 'client_id'], condition=~models.Q(client_id=''),
                                    name='uniq_chat_msg_client_id'),
        ]

    def __str__(self):
        return f"{self.sender_side}: {self.body[:40]}"


class ChatViolation(models.Model):
    """
    A message refused for trying to take the deal off Negromart (contact details,
    links, outside payment...). Kept for staff review and to mute repeat offenders.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_violations')
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='violations')
    side = models.CharField(max_length=8, choices=Side.CHOICES)
    categories = models.JSONField(default=list)
    excerpt = models.TextField(max_length=1000, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', '-created_at'], name='chat_violation_user_idx')]

    def __str__(self):
        return f"{self.user_id} {', '.join(self.categories)} @ {self.created_at:%Y-%m-%d %H:%M}"

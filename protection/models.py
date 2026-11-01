"""
Negromart Guarantee: when an item doesn't arrive or isn't what was ordered,
the shopper opens a claim. The seller has RESPOND_HOURS to make it right
(offer a refund); otherwise, or if the shopper isn't satisfied, Negromart
decides. While a claim is open the seller's earnings for that line are held
out of payouts (payments/ledger.reserve_payout).

    open ──seller offers refund──▶ seller_offered ──shopper accepts──▶ resolved_buyer
      │                                  └──shopper escalates──▶ escalated
      ├──no reply in time / shopper escalates──▶ escalated ──staff──▶ resolved_buyer | resolved_seller
      └──shopper withdraws──▶ withdrawn
"""
import secrets

from django.conf import settings
from django.db import models


def _reference():
    return 'CLM-' + secrets.token_hex(4).upper()


class Claim(models.Model):
    NOT_RECEIVED = 'not_received'
    NOT_AS_DESCRIBED = 'not_as_described'
    DAMAGED = 'damaged'
    COUNTERFEIT = 'counterfeit'
    REASON_CHOICES = [
        (NOT_RECEIVED, "Item didn't arrive"),
        (NOT_AS_DESCRIBED, 'Not as described'),
        (DAMAGED, 'Arrived damaged or not working'),
        (COUNTERFEIT, 'Fake or counterfeit'),
    ]

    OPEN = 'open'
    SELLER_OFFERED = 'seller_offered'
    ESCALATED = 'escalated'
    RESOLVED_BUYER = 'resolved_buyer'
    RESOLVED_SELLER = 'resolved_seller'
    WITHDRAWN = 'withdrawn'
    STATUS_CHOICES = [
        (OPEN, 'Waiting for the seller'),
        (SELLER_OFFERED, 'Seller offered a refund'),
        (ESCALATED, 'Negromart is reviewing'),
        (RESOLVED_BUYER, 'Refunded'),
        (RESOLVED_SELLER, 'Closed in the seller’s favour'),
        (WITHDRAWN, 'Withdrawn'),
    ]
    ACTIVE = (OPEN, SELLER_OFFERED, ESCALATED)

    reference = models.CharField(max_length=16, unique=True, default=_reference, editable=False)
    order = models.ForeignKey('order.Order', on_delete=models.PROTECT, related_name='claims')
    order_product = models.ForeignKey('order.OrderProduct', on_delete=models.PROTECT, related_name='claims')
    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.SET_NULL, null=True, related_name='claims')
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='claims')
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    details = models.TextField(max_length=2000)
    amount_claimed = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=OPEN, db_index=True)

    seller_respond_by = models.DateTimeField()
    seller_offer_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    seller_offered_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    escalation_reason = models.CharField(max_length=40, blank=True, default='')

    resolution_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    resolution_note = models.TextField(max_length=2000, blank=True, default='')
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolved_at = models.DateTimeField(null=True, blank=True)
    # Money back to the shopper is sent by finance; this records when.
    refund_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        constraints = [
            # One active claim per order line.
            models.UniqueConstraint(fields=['order_product'], condition=models.Q(status__in=['open', 'seller_offered', 'escalated']),
                                    name='uniq_active_claim_per_line'),
        ]
        indexes = [
            models.Index(fields=['vendor', 'status'], name='claim_vendor_status_idx'),
            models.Index(fields=['status', 'seller_respond_by'], name='claim_deadline_idx'),
        ]

    @property
    def is_active(self):
        return self.status in self.ACTIVE

    def __str__(self):
        return self.reference


class ClaimMessage(models.Model):
    BUYER, SELLER, STAFF, SYSTEM = 'buyer', 'seller', 'staff', 'system'
    SIDE_CHOICES = [(BUYER, 'Shopper'), (SELLER, 'Seller'), (STAFF, 'Negromart'), (SYSTEM, 'Update')]

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name='messages')
    side = models.CharField(max_length=8, choices=SIDE_CHOICES)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    text = models.TextField(max_length=2000)
    image = models.ImageField(upload_to='claims/%Y/%m/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('created_at',)

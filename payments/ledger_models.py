"""
payments/ledger_models.py
The seller money ledger: every amount that changes what Negromart owes a store.

Why a ledger instead of computing payouts from orders: each line is written
once and never edited, so a seller's balance, statement and every payout can
be explained line by line, and refunds after a payout simply carry forward as
a negative amount. Posting rules live in payments/ledger.py.

Sign convention: positive = owed to the seller, negative = owed by the seller.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q


class LedgerEntry(models.Model):
    SALE = 'sale'
    COMMISSION = 'commission'
    DELIVERY_EARNING = 'delivery_earning'
    REFUND = 'refund'
    COMMISSION_REFUND = 'commission_refund'
    PAYOUT = 'payout'
    ADJUSTMENT = 'adjustment'
    TYPE_CHOICES = [
        (SALE, 'Sale'),
        (COMMISSION, 'Commission'),
        (DELIVERY_EARNING, 'Delivery earning'),
        (REFUND, 'Refund to customer'),
        (COMMISSION_REFUND, 'Commission returned'),
        (PAYOUT, 'Payout'),
        (ADJUSTMENT, 'Adjustment'),
    ]
    # Types tied to one order line, at most once per line (idempotent posting).
    PER_LINE_TYPES = (SALE, COMMISSION, DELIVERY_EARNING, REFUND, COMMISSION_REFUND)

    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.PROTECT, related_name='ledger_entries')
    entry_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='GHS')
    description = models.CharField(max_length=255)

    order = models.ForeignKey('order.Order', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    order_product = models.ForeignKey('order.OrderProduct', on_delete=models.PROTECT, null=True, blank=True,
                                      related_name='ledger_entries')
    return_request = models.ForeignKey('order.ReturnRequest', on_delete=models.PROTECT, null=True, blank=True,
                                       related_name='+')
    # Commission % applied (snapshot of the plan at posting time), for COMMISSION rows.
    rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    # Earnings are held until this moment (return window / plan payout delay).
    available_at = models.DateTimeField(db_index=True)
    # The payout that settled this entry. NULL = still part of the balance.
    payout = models.ForeignKey('payments.Payout', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='ledger_entries')

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', help_text="Staff member, for manual adjustments.")

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name_plural = 'Ledger entries'
        indexes = [
            models.Index(fields=['vendor', 'payout', 'available_at'], name='ledger_balance_idx'),
            models.Index(fields=['vendor', '-created_at'], name='ledger_statement_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['order_product', 'entry_type'],
                condition=Q(entry_type__in=('sale', 'commission', 'delivery_earning',
                                            'refund', 'commission_refund')),
                name='uniq_ledger_entry_per_line_and_type',
            ),
        ]

    def __str__(self):
        return f"{self.get_entry_type_display()} {self.amount} {self.currency} ({self.vendor_id})"

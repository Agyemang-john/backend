"""
Ledger postings for buyer-protection claims (protection/).

A claim decided for the shopper debits the seller like a refunded return:
− REFUND for the amount, + COMMISSION_REFUND for the matching share. If the
line was never delivered (and so never earned), nothing is debited: the line
is cancelled instead, so its sale is never posted.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .ledger import _money, _post_once, _title, post_shipment_earnings
from .ledger_models import LedgerEntry


def post_claim_refund(claim):
    line = claim.order_product
    vendor = claim.vendor
    if vendor is None:
        return
    for shipment in line.shipments.filter(status='delivered', ledger_posted_at__isnull=True):
        post_shipment_earnings(shipment.pk)

    refund = _money(claim.resolution_amount or 0)
    if refund <= 0:
        return
    with transaction.atomic():
        sale = LedgerEntry.objects.filter(order_product=line, entry_type=LedgerEntry.SALE).first()
        if sale is None:
            # Never delivered, never earned: make sure it never will be.
            type(line).objects.filter(pk=line.pk).update(status='canceled')
            return
        now = timezone.now()
        _post_once(vendor, LedgerEntry.REFUND, -refund, line, now, f"Claim {claim.reference}: refund for {_title(line)}")
        commission = LedgerEntry.objects.filter(order_product=line, entry_type=LedgerEntry.COMMISSION).first()
        if commission and line.amount:
            share = _money(-commission.amount * refund / Decimal(line.amount))
            if share > 0:
                _post_once(vendor, LedgerEntry.COMMISSION_REFUND, share, line, now,
                           f"Commission returned for claim {claim.reference}", rate=commission.rate)

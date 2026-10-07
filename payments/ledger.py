"""
payments/ledger.py
Posting rules for the seller ledger (payments/ledger_models.py) and payouts.

When                                   Entries (+ owed to seller, − owed by seller)
─────────────────────────────────────  ────────────────────────────────────────────
Shipment delivered                     + SALE per line, − COMMISSION per line,
                                       + DELIVERY_EARNING if the seller delivered it
                                       (held until delivered + plan payout delay)
Return refunded                        − REFUND, + COMMISSION_REFUND (pro rata)
Payout sent                            − PAYOUT (settles everything it covered)
Staff adjustment                       ± ADJUSTMENT

Every posting function is idempotent (unique per line + type, row locks), so
it can be called from request handlers and from the periodic sweep alike.
"""

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Min, Q, Sum
from django.utils import timezone

from .entitlements import commission_rate, payout_delay_days
from .ledger_models import LedgerEntry
from .models import Payout

logger = logging.getLogger('payouts')

CENT = Decimal('0.01')
MIN_PAYOUT_AMOUNT = Decimal(str(getattr(settings, 'SELLER_MIN_PAYOUT_AMOUNT', '10.00')))


def _money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# ── Earnings ──────────────────────────────────────────────────────────────────

def post_shipment_earnings(shipment_id):
    """Post a delivered shipment's sale, commission and delivery entries once."""
    from order.models import Shipment

    with transaction.atomic():
        # Lock only the shipment row: vendor/order are nullable FKs (outer
        # joins), which Postgres can't lock.
        shipment = (
            Shipment.objects.select_for_update(of=('self',))
            .select_related('vendor', 'order')
            .filter(pk=shipment_id).first()
        )
        if shipment is None or shipment.vendor_id is None:
            return False
        if shipment.status != 'delivered' or shipment.ledger_posted_at:
            return False

        vendor = shipment.vendor
        delivered_at = shipment.delivered_at or timezone.now()
        available_at = delivered_at + timedelta(days=payout_delay_days(vendor))
        rate = commission_rate(vendor)

        lines = list(shipment.items.exclude(status='canceled').select_related('product'))
        for line in lines:
            sale = _money(line.amount)
            _post_once(vendor, LedgerEntry.SALE, sale, line, available_at,
                       f"Sale of {line.quantity} × {_title(line)}")
            _post_once(vendor, LedgerEntry.COMMISSION, -_money(sale * rate / 100), line, available_at,
                       f"Commission {rate}% on {_title(line)}", rate=rate)

        # Delivery fees belong to whoever made the delivery. Only when the
        # seller delivered it themselves does the fee become their earning.
        if shipment.fulfilled_by == 'seller' and lines:
            fee = _seller_delivery_fee(shipment)
            if fee > 0:
                _post_once(vendor, LedgerEntry.DELIVERY_EARNING, fee, lines[0], available_at,
                           f"Delivery fee, order {shipment.order.order_number}")

        shipment.ledger_posted_at = timezone.now()
        shipment.save(update_fields=['ledger_posted_at'])
    logger.info("ledger: posted shipment=%s vendor=%s lines=%s", shipment_id, vendor.pk, len(lines))
    return True


def post_refund(return_request):
    """Debit the seller for a refunded return (and give back its commission share)."""
    line = return_request.order_product
    vendor = return_request.vendor
    if vendor is None:
        return

    # A return implies delivery; make sure the original sale is on the books
    # first so the refund never lands without its matching sale.
    for shipment in line.shipments.filter(status='delivered', ledger_posted_at__isnull=True):
        post_shipment_earnings(shipment.pk)

    refund = _money(return_request.refund_amount)
    now = timezone.now()
    with transaction.atomic():
        _post_once(vendor, LedgerEntry.REFUND, -refund, line, now,
                   f"Refund {return_request.reference} for {_title(line)}",
                   return_request=return_request)

        commission = LedgerEntry.objects.filter(order_product=line, entry_type=LedgerEntry.COMMISSION).first()
        if commission and line.amount:
            share = _money(-commission.amount * refund / Decimal(line.amount))
            if share > 0:
                _post_once(vendor, LedgerEntry.COMMISSION_REFUND, share, line, now,
                           f"Commission returned for refund {return_request.reference}",
                           rate=commission.rate, return_request=return_request)


def post_adjustment(vendor, amount, description, staff_user):
    """Manual correction by Negromart staff; available immediately."""
    return LedgerEntry.objects.create(
        vendor=vendor, entry_type=LedgerEntry.ADJUSTMENT, amount=_money(amount),
        description=description[:255], available_at=timezone.now(), created_by=staff_user,
    )


def _post_once(vendor, entry_type, amount, line, available_at, description, rate=None, return_request=None):
    entry, created = LedgerEntry.objects.get_or_create(
        order_product=line, entry_type=entry_type,
        defaults=dict(
            vendor=vendor, amount=amount, order_id=line.order_id, available_at=available_at,
            description=description[:255], rate=rate, return_request=return_request,
        ),
    )
    return entry


def _title(line):
    return line.product.title if line.product else 'deleted product'


def _seller_delivery_fee(shipment):
    try:
        result = shipment.order.calculate_vendor_delivery_fee(shipment.vendor)
        return _money(getattr(result, 'total', result) or 0)
    except Exception:
        logger.exception("ledger: delivery fee lookup failed for shipment=%s", shipment.pk)
        return Decimal('0.00')


# ── Balances ──────────────────────────────────────────────────────────────────

def balance_summary(vendor):
    """
    available  – can be paid out now
    pending    – earned, still inside the hold period
    processing – reserved by a payout that is in flight
    paid_total – everything paid out so far
    """
    now = timezone.now()
    unsettled = LedgerEntry.objects.filter(vendor=vendor, payout__isnull=True)
    agg = unsettled.aggregate(
        available=Sum('amount', filter=Q(available_at__lte=now)),
        pending=Sum('amount', filter=Q(available_at__gt=now)),
        next_release=Min('available_at', filter=Q(available_at__gt=now)),
    )
    payouts = Payout.objects.filter(vendor=vendor).aggregate(
        processing=Sum('amount', filter=Q(status='processing')),
        paid_total=Sum('amount', filter=Q(status='success')),
    )
    zero = Decimal('0.00')
    return {
        'currency': 'GHS',
        'available': agg['available'] or zero,
        'pending': agg['pending'] or zero,
        'processing': payouts['processing'] or zero,
        'paid_total': payouts['paid_total'] or zero,
        'next_release_at': agg['next_release'],
        'minimum_payout': MIN_PAYOUT_AMOUNT,
    }


# ── Payouts ───────────────────────────────────────────────────────────────────

def reserve_payout(vendor):
    """
    Lock the store's available entries into a new 'processing' Payout.
    Returns the Payout, or None if there is nothing (or too little) to pay.
    """
    from vendor.models import Vendor

    now = timezone.now()
    with transaction.atomic():
        Vendor.objects.select_for_update().filter(pk=vendor.pk).first()  # one payout at a time per store
        entries = LedgerEntry.objects.select_for_update().filter(
            vendor=vendor, payout__isnull=True, available_at__lte=now,
        )
        totals = entries.aggregate(
            total=Sum('amount'),
            sales=Sum('amount', filter=Q(entry_type=LedgerEntry.SALE)),
            delivery=Sum('amount', filter=Q(entry_type=LedgerEntry.DELIVERY_EARNING)),
        )
        total = totals['total'] or Decimal('0.00')
        if total < MIN_PAYOUT_AMOUNT:
            return None

        payout = Payout.objects.create(
            vendor=vendor, amount=total, status='processing',
            product_total=totals['sales'] or 0, delivery_fee=totals['delivery'] or 0,
        )
        order_ids = set(entries.exclude(order__isnull=True).values_list('order_id', flat=True))
        entries.update(payout=payout)
        payout.order.set(order_ids)
    return payout


def complete_payout(payout, *, success, transaction_id=None, error=None):
    """Record the transfer result. Failure releases the entries back to the balance."""
    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(pk=payout.pk)
        if payout.status != 'processing':
            return payout
        if success:
            payout.status = 'success'
            payout.transaction_id = transaction_id
            LedgerEntry.objects.create(
                vendor_id=payout.vendor_id, entry_type=LedgerEntry.PAYOUT, amount=-payout.amount,
                description=f"Payout {transaction_id or payout.pk}", available_at=timezone.now(),
                payout=payout,
            )
        else:
            payout.status = 'failed'
            payout.error_message = error
            LedgerEntry.objects.filter(payout=payout).update(payout=None)
        payout.save(update_fields=['status', 'transaction_id', 'error_message', 'updated_at'])
    return payout


def pay_vendor(vendor):
    """Reserve, transfer and settle one store's available balance."""
    from .payout_service import PayoutService

    payout = reserve_payout(vendor)
    if payout is None:
        return None
    result = PayoutService().send_transfer(vendor, payout.amount, f"Negromart payout #{payout.pk}")
    payout = complete_payout(
        payout,
        success=result.get('status') == 'success',
        transaction_id=result.get('transaction_id'),
        error=result.get('message'),
    )
    _notify_payout(vendor, payout)
    return payout


def _notify_payout(vendor, payout):
    if payout.status != 'success' or not vendor.user_id:
        return
    try:
        from notification.utils import send_notification
        send_notification(
            recipient=vendor.user, verb='vendor_payout', target=payout,
            data={'amount': str(payout.amount), 'message': f"GHS {payout.amount} has been sent to your payout account.",
                  'url': '/payouts'},
        )
    except Exception:
        logger.exception("ledger: payout notification failed payout=%s", payout.pk)

"""
order/returns.py
Return rules and state changes. Views stay thin and call these.

Lifecycle
    requested ──seller approves──▶ approved ──seller confirms receipt──▶ received ──Negromart refunds──▶ refunded
        │
        ├──seller rejects (with reason)──▶ rejected
        └──customer cancels (before receipt)──▶ cancelled

Who may do what
    customer : open (within the window), cancel while requested/approved
    seller   : approve, reject (note required), mark received
    staff    : refund (money leaves the platform here; the seller's ledger is
               debited by payments.ledger.post_refund)

Return window
    Change of mind / other : the product's return_period_days (0 = not accepted)
    Seller at fault        : at least RETURN_MIN_WINDOW_SELLER_FAULT_DAYS
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ReturnRequest


class ReturnError(Exception):
    """A rule was broken. The message is safe to show to the user."""


@dataclass
class Eligibility:
    allowed: bool
    message: str = ''
    window_ends_at: object = None


def _delivered_at(line):
    if line.delivered_date:
        return line.delivered_date
    shipment = line.shipments.filter(status='delivered').order_by('-delivered_at').first()
    return shipment.delivered_at if shipment else None


def return_window_days(line, reason):
    product_days = line.product.return_period_days if line.product else 0
    if reason in ReturnRequest.SELLER_FAULT_REASONS:
        return max(product_days, settings.RETURN_MIN_WINDOW_SELLER_FAULT_DAYS)
    return product_days


def check_eligibility(line, customer, reason, now=None):
    now = now or timezone.now()
    if line.order.user_id != getattr(customer, 'pk', None):
        return Eligibility(False, "This item is not in one of your orders.")
    if line.status == 'canceled':
        return Eligibility(False, "This item was cancelled.")
    delivered_at = _delivered_at(line)
    if not delivered_at:
        return Eligibility(False, "You can request a return once the item has been delivered.")
    if line.return_requests.filter(status__in=ReturnRequest.OPEN_STATUSES).exists():
        return Eligibility(False, "There is already an open return for this item.")
    if line.return_requests.filter(status=ReturnRequest.STATUS_REFUNDED).exists():
        return Eligibility(False, "This item has already been refunded.")

    days = return_window_days(line, reason)
    if days <= 0:
        return Eligibility(False, "This product can only be returned if it arrived damaged, defective or "
                                  "different from what you ordered.")
    window_ends_at = delivered_at + timedelta(days=days)
    if now > window_ends_at:
        return Eligibility(False, f"The return window for this item closed on {window_ends_at:%d %b %Y}.",
                           window_ends_at)
    return Eligibility(True, window_ends_at=window_ends_at)


def _unit_price(line):
    if line.quantity:
        return (Decimal(line.amount) / line.quantity).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return Decimal(line.price)


# ── Customer ──────────────────────────────────────────────────────────────────

def open_return(*, customer, line, reason, details='', quantity=1):
    if reason not in dict(ReturnRequest.REASON_CHOICES):
        raise ReturnError("Choose a reason for the return.")
    quantity = int(quantity or 1)
    if quantity < 1 or quantity > line.quantity:
        raise ReturnError(f"You can return between 1 and {line.quantity} of this item.")
    if reason == 'other' and not (details or '').strip():
        raise ReturnError("Please describe the problem.")

    eligibility = check_eligibility(line, customer, reason)
    if not eligibility.allowed:
        raise ReturnError(eligibility.message)

    vendor = line.product.vendor if line.product else None
    try:
        with transaction.atomic():
            request = ReturnRequest.objects.create(
                order_product=line, order=line.order, vendor=vendor, customer=customer,
                reason=reason, details=(details or '').strip(), quantity=quantity,
                refund_amount=_unit_price(line) * quantity,
            )
    except IntegrityError:
        raise ReturnError("There is already an open return for this item.")

    _notify_vendor(request)
    return request


def cancel_return(request, customer):
    if request.customer_id != customer.pk:
        raise ReturnError("This return is not yours.")
    _transition(request, {ReturnRequest.STATUS_REQUESTED, ReturnRequest.STATUS_APPROVED},
                ReturnRequest.STATUS_CANCELLED)
    return request


# ── Seller ────────────────────────────────────────────────────────────────────

def approve(request, user, note=''):
    _transition(request, {ReturnRequest.STATUS_REQUESTED}, ReturnRequest.STATUS_APPROVED,
                decided_by=user, decided_at=timezone.now(), seller_note=(note or '').strip())
    _book_pickup(request)
    _notify_customer(request, "Your return was approved. We'll arrange collection of the item.")
    return request


def reject(request, user, note):
    note = (note or '').strip()
    if len(note) < 10:
        raise ReturnError("Please explain to the customer why the return is rejected.")
    _transition(request, {ReturnRequest.STATUS_REQUESTED}, ReturnRequest.STATUS_REJECTED,
                decided_by=user, decided_at=timezone.now(), seller_note=note)
    _notify_customer(request, f"Your return was not accepted: {note}")
    return request


def mark_received(request, user):
    _transition(request, {ReturnRequest.STATUS_APPROVED}, ReturnRequest.STATUS_RECEIVED,
                received_at=timezone.now())
    _notify_customer(request, "The seller has received your returned item. Your refund is being processed.")
    return request


# ── Negromart staff ───────────────────────────────────────────────────────────

def mark_refunded(request, staff_user):
    """Record that the customer's money was returned and debit the seller."""
    from payments.ledger import post_refund

    _transition(request, {ReturnRequest.STATUS_RECEIVED}, ReturnRequest.STATUS_REFUNDED,
                refunded_at=timezone.now())
    post_refund(request)
    _notify_customer(request, f"Your refund of GHS {request.refund_amount} has been processed.")
    return request


# ── Internals ─────────────────────────────────────────────────────────────────

def _transition(request, allowed_from, to_status, **fields):
    with transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=request.pk)
        if locked.status not in allowed_from:
            raise ReturnError(f"This return is {locked.get_status_display().lower()} and can't be changed that way.")
        locked.status = to_status
        for key, value in fields.items():
            setattr(locked, key, value)
        locked.save()
    request.refresh_from_db()


def _book_pickup(request):
    """Ask the delivery provider to collect the item (platform: handled by the team)."""
    from .delivery import DeliveryProviderError, default_provider
    try:
        default_provider().book_return_pickup(request)
    except DeliveryProviderError:
        pass


def _notify_vendor(request):
    vendor = request.vendor
    if not vendor or not vendor.user_id:
        return
    from notification.utils import send_notification
    send_notification(
        recipient=vendor.user, verb='vendor_return_requested', target=request,
        data={
            'reference': request.reference,
            'message': f"Return requested for order {request.order.order_number}: {request.get_reason_display()}.",
            'url': f'/order-returns?ref={request.reference}',
        },
    )


def _notify_customer(request, message):
    if not request.customer_id:
        return
    from notification.utils import send_notification
    send_notification(
        recipient=request.customer, verb='customer_return_update', target=request,
        data={'reference': request.reference, 'status': request.status, 'message': message,
              'url': f'/dashboard/order-history/{request.order_id}/'},
    )

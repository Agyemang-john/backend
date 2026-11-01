"""Claim rules and state changes; views and admin call these."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Claim, ClaimMessage

RESPOND_HOURS = getattr(settings, 'CLAIM_SELLER_RESPOND_HOURS', 48)
CLAIM_WINDOW_DAYS = getattr(settings, 'CLAIM_WINDOW_DAYS', 30)          # after delivery
NOT_RECEIVED_AFTER_DAYS = getattr(settings, 'CLAIM_NOT_RECEIVED_AFTER_DAYS', 10)   # after ordering
BUYER_DECIDE_DAYS = 5


class ClaimError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass
class Eligibility:
    ok: bool
    message: str = ''
    reasons: tuple = ()


def _delivered_at(line):
    from order.returns import _delivered_at as delivered
    return delivered(line)


def eligibility(line, user, now=None):
    """Which claim reasons this shopper can use for this order line right now."""
    from order.models import ReturnRequest
    now = now or timezone.now()
    order = line.order
    if order.user_id != getattr(user, 'pk', None) or not order.is_ordered:
        return Eligibility(False, "This item is not in one of your orders.")
    if line.status == 'canceled':
        return Eligibility(False, "This item was cancelled.")
    if line.claims.filter(status__in=Claim.ACTIVE).exists():
        return Eligibility(False, "There's already an open claim for this item.")
    if line.claims.filter(status__in=(Claim.RESOLVED_BUYER, Claim.RESOLVED_SELLER)).exists():
        return Eligibility(False, "A claim for this item has already been decided.")
    if line.return_requests.filter(status__in=list(ReturnRequest.OPEN_STATUSES) + [ReturnRequest.STATUS_REFUNDED]).exists():
        return Eligibility(False, "This item has a return in progress or was already refunded.")

    delivered = _delivered_at(line)
    if delivered is None:
        if order.payment_method == 'cash_on_delivery':
            return Eligibility(False, "Pay-on-delivery orders aren't charged until they arrive.")
        due = getattr(line, 'promised_by', None)
        if due:
            late_from = timezone.make_aware(datetime.combine(due + timedelta(days=1), time.min))
        else:
            late_from = order.date_created + timedelta(days=NOT_RECEIVED_AFTER_DAYS)
        if now < late_from:
            return Eligibility(False, f"If it hasn't arrived by {late_from:%d %b %Y}, you can open a claim.")
        return Eligibility(True, reasons=(Claim.NOT_RECEIVED,))
    if now > delivered + timedelta(days=CLAIM_WINDOW_DAYS):
        return Eligibility(False, f"Claims must be opened within {CLAIM_WINDOW_DAYS} days of delivery.")
    return Eligibility(True, reasons=(Claim.NOT_AS_DESCRIBED, Claim.DAMAGED, Claim.COUNTERFEIT))


def _say(claim, side, text, author=None, image=None):
    return ClaimMessage.objects.create(claim=claim, side=side, text=text[:2000], author=author, image=image)


def open_claim(user, line, reason, details, amount=None):
    check = eligibility(line, user)
    if not check.ok:
        raise ClaimError(check.message, 409)
    if reason not in check.reasons:
        raise ClaimError("That reason doesn't apply to this item right now.")
    details = (details or '').strip()
    if len(details) < 20:
        raise ClaimError("Tell us what happened in at least 20 characters.")
    amount = Decimal(line.amount) if amount in (None, '') else Decimal(str(amount))
    if amount <= 0 or amount > Decimal(line.amount):
        raise ClaimError(f"Claim between GHS 0.01 and GHS {line.amount}.")
    try:
        with transaction.atomic():
            claim = Claim.objects.create(
                order=line.order, order_product=line, vendor=line.product.vendor if line.product else None,
                buyer=user, reason=reason, details=details, amount_claimed=amount,
                seller_respond_by=timezone.now() + timedelta(hours=RESPOND_HOURS),
            )
            _say(claim, ClaimMessage.BUYER, details, author=user)
    except IntegrityError:
        raise ClaimError("There's already an open claim for this item.", 409)
    transaction.on_commit(lambda: _notify(claim, 'opened'))
    return claim


def add_message(claim, side, user, text, image=None):
    if not claim.is_active:
        raise ClaimError("This claim is closed.")
    text = (text or '').strip()
    if not text and not image:
        raise ClaimError("Write a message or add a photo.")
    msg = _say(claim, side, text, author=user, image=image)
    transaction.on_commit(lambda: _notify(claim, f'message_{side}'))
    return msg


def seller_offer(claim, user, amount, note=''):
    if claim.status != Claim.OPEN:
        raise ClaimError("You can only respond while the claim is waiting for you.")
    amount = Decimal(str(amount))
    if amount <= 0 or amount > Decimal(claim.order_product.amount):
        raise ClaimError(f"Offer between GHS 0.01 and GHS {claim.order_product.amount}.")
    claim.status, claim.seller_offer_amount, claim.seller_offered_at = Claim.SELLER_OFFERED, amount, timezone.now()
    claim.save(update_fields=['status', 'seller_offer_amount', 'seller_offered_at', 'updated_at'])
    _say(claim, ClaimMessage.SELLER, note or f"I'm offering a refund of GHS {amount}.", author=user)
    # A full refund is accepted straight away: nothing for the shopper to decide.
    if amount >= claim.amount_claimed:
        return resolve_for_buyer(claim, amount, None, "Seller agreed to refund in full.")
    transaction.on_commit(lambda: _notify(claim, 'offered'))
    return claim


def buyer_accept(claim):
    if claim.status != Claim.SELLER_OFFERED:
        raise ClaimError("There's no offer to accept.")
    return resolve_for_buyer(claim, claim.seller_offer_amount, None, "Shopper accepted the seller's offer.")


def escalate(claim, why, user=None):
    if claim.status not in (Claim.OPEN, Claim.SELLER_OFFERED):
        raise ClaimError("This claim can't be escalated now.")
    if why == 'buyer' and claim.status == Claim.OPEN and timezone.now() < claim.seller_respond_by:
        raise ClaimError(f"The seller has until {claim.seller_respond_by:%d %b %H:%M} to respond.")
    claim.status, claim.escalated_at, claim.escalation_reason = Claim.ESCALATED, timezone.now(), why
    claim.save(update_fields=['status', 'escalated_at', 'escalation_reason', 'updated_at'])
    text = {"buyer": "The shopper asked Negromart to step in.",
            "seller_timeout": "The seller didn't respond in time, so Negromart is reviewing the claim.",
            "buyer_timeout": "The offer wasn't accepted in time, so Negromart is reviewing the claim."}.get(why, "Negromart is reviewing.")
    _say(claim, ClaimMessage.SYSTEM, text, author=user)
    transaction.on_commit(lambda: _notify(claim, 'escalated'))
    return claim


def withdraw(claim, user):
    if not claim.is_active:
        raise ClaimError("This claim is already closed.")
    claim.status = Claim.WITHDRAWN
    claim.save(update_fields=['status', 'updated_at'])
    _say(claim, ClaimMessage.SYSTEM, "The shopper withdrew the claim.", author=user)
    transaction.on_commit(lambda: _notify(claim, 'withdrawn'))
    return claim


@transaction.atomic
def resolve_for_buyer(claim, amount, staff_user, note):
    from payments.claims_ledger import post_claim_refund
    claim = Claim.objects.select_for_update().get(pk=claim.pk)
    if not claim.is_active:
        raise ClaimError("This claim is already closed.")
    amount = min(Decimal(str(amount)), Decimal(claim.order_product.amount))
    claim.status, claim.resolution_amount = Claim.RESOLVED_BUYER, amount
    claim.resolution_note, claim.resolved_by, claim.resolved_at = note[:2000], staff_user, timezone.now()
    claim.save()
    post_claim_refund(claim)
    _say(claim, ClaimMessage.SYSTEM, f"Resolved: GHS {amount} will be refunded to the shopper.", author=staff_user)
    transaction.on_commit(lambda: _notify(claim, 'resolved_buyer'))
    return claim


def resolve_for_seller(claim, staff_user, note):
    if not claim.is_active:
        raise ClaimError("This claim is already closed.")
    if len((note or '').strip()) < 10:
        raise ClaimError("Explain the decision for the shopper.")
    claim.status, claim.resolution_amount = Claim.RESOLVED_SELLER, Decimal('0')
    claim.resolution_note, claim.resolved_by, claim.resolved_at = note[:2000], staff_user, timezone.now()
    claim.save()
    _say(claim, ClaimMessage.STAFF, note, author=staff_user)
    transaction.on_commit(lambda: _notify(claim, 'resolved_seller'))
    return claim


def run_deadlines(now=None):
    """Hourly: escalate claims the seller ignored, and offers the shopper never answered."""
    now = now or timezone.now()
    n = 0
    for claim in Claim.objects.filter(status=Claim.OPEN, seller_respond_by__lt=now)[:500]:
        escalate(claim, 'seller_timeout')
        n += 1
    stale = now - timedelta(days=BUYER_DECIDE_DAYS)
    for claim in Claim.objects.filter(status=Claim.SELLER_OFFERED, seller_offered_at__lt=stale)[:500]:
        escalate(claim, 'buyer_timeout')
        n += 1
    return n


# ── Notifications ────────────────────────────────────────────────────────────

def _notify(claim, event):
    from notification.utils import send_notification
    from vendor.models import VendorMember

    title = claim.order_product.product.title if claim.order_product.product else 'your item'
    buyer_url = f"/dashboard/claims/{claim.reference}"
    seller_url = f"/claims/{claim.reference}"
    to_buyer = {
        'offered': f"The seller offered GHS {claim.seller_offer_amount} for {title}. Accept it or ask Negromart to step in.",
        'escalated': f"Negromart is reviewing your claim {claim.reference}.",
        'resolved_buyer': f"Claim {claim.reference} resolved: GHS {claim.resolution_amount} will be refunded.",
        'resolved_seller': f"Claim {claim.reference} was closed. See Negromart's decision.",
        'message_seller': f"The seller replied about {title}.",
        'message_staff': f"Negromart replied on claim {claim.reference}.",
    }
    to_seller = {
        'opened': f"A shopper opened claim {claim.reference} about {title}. Respond by {claim.seller_respond_by:%d %b %H:%M}.",
        'escalated': f"Claim {claim.reference} is now with Negromart.",
        'resolved_buyer': f"Claim {claim.reference} resolved: GHS {claim.resolution_amount} refunded from your earnings.",
        'resolved_seller': f"Claim {claim.reference} was decided in your favour.",
        'withdrawn': f"The shopper withdrew claim {claim.reference}.",
        'message_buyer': f"New message on claim {claim.reference}.",
        'message_staff': f"Negromart replied on claim {claim.reference}.",
    }
    if event in to_buyer and claim.buyer_id:
        send_notification(claim.buyer, 'customer_claim_update', target=claim,
                          data={'message': to_buyer[event], 'url': buyer_url, 'reference': claim.reference})
    if event in to_seller and claim.vendor_id:
        for m in VendorMember.objects.filter(vendor_id=claim.vendor_id, is_active=True, role__in=['owner', 'admin']).select_related('user'):
            send_notification(m.user, 'vendor_claim_update', target=claim,
                              data={'message': to_seller[event], 'url': seller_url, 'reference': claim.reference})

"""Scoring orders, holds, staff decisions and the pay-on-delivery gate."""
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import OrderRisk
from .rules import refused_cod_count, score

logger = logging.getLogger(__name__)

COD_BLOCK_AFTER_REFUSALS = 2


class RiskError(Exception):
    pass


def assess(order):
    """Score once (idempotent). Returns the OrderRisk."""
    existing = OrderRisk.objects.filter(order=order).first()
    if existing:
        return existing
    total, level, reasons = score(order)
    status = OrderRisk.HELD if level == OrderRisk.HIGH else OrderRisk.CLEAR
    try:
        with transaction.atomic():
            risk = OrderRisk.objects.create(order=order, score=total, level=level, reasons=reasons, status=status)
    except IntegrityError:
        return OrderRisk.objects.get(order=order)
    if status == OrderRisk.HELD:
        logger.warning("order %s held for review: score %s %s", order.pk, total, [r['code'] for r in reasons])
    return risk


def is_held(order_id):
    return OrderRisk.objects.filter(order_id=order_id, status=OrderRisk.HELD).exists()


def approve(risk, staff_user, note=''):
    if risk.status != OrderRisk.HELD:
        raise RiskError("Only held orders can be approved.")
    risk.status, risk.reviewed_by, risk.reviewed_at, risk.note = OrderRisk.APPROVED, staff_user, timezone.now(), note[:255]
    risk.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'note'])
    _notify_sellers(risk.order, "Order {number} has been checked and can be shipped.")
    return risk


@transaction.atomic
def cancel_as_fraud(risk, staff_user, note=''):
    """Cancel every open line, put stock back, and queue refunds for prepaid orders."""
    from order.models import Refund
    from order.sourcing import _restock

    if risk.status not in (OrderRisk.HELD, OrderRisk.CLEAR):
        raise RiskError("This order was already reviewed.")
    order = risk.order
    for line in order.order_products.select_for_update(of=('self',)).exclude(status='canceled'):
        if line.shipments.exclude(status='canceled').exists():
            continue   # already with a courier: handle manually
        line.status = 'canceled'
        line.refund_reason = 'Cancelled after a security review'
        line.save(update_fields=['status', 'refund_reason'])
        _restock(line)
        if order.payment_method != 'cash_on_delivery':
            Refund.objects.create(order_product=line, amount=float(line.amount), reason=line.refund_reason)
    order.status = 'canceled'
    order.save(update_fields=['status'])
    risk.status, risk.reviewed_by, risk.reviewed_at, risk.note = OrderRisk.CANCELLED, staff_user, timezone.now(), note[:255]
    risk.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'note'])
    transaction.on_commit(lambda: _notify_sellers(order, "Order {number} was cancelled by Negromart. Don't ship it."))
    return risk


def cod_allowed(user):
    """(allowed, message). Pay-on-delivery is withdrawn after repeated refusals."""
    if refused_cod_count(user) >= COD_BLOCK_AFTER_REFUSALS:
        return False, ("Pay on delivery isn't available on your account because earlier pay-on-delivery "
                       "orders weren't accepted. Please pay online.")
    if OrderRisk.objects.filter(order__user=user, status=OrderRisk.HELD).exists():
        return False, "You have an order under review. Please pay online, or wait until it's checked."
    return True, ''


def _notify_sellers(order, template):
    from notification.utils import send_notification
    from vendor.models import VendorMember
    vendor_ids = set(order.order_products.values_list('product__vendor_id', flat=True)) - {None}
    for m in VendorMember.objects.filter(vendor_id__in=vendor_ids, is_active=True, role__in=['owner', 'admin']).select_related('user'):
        send_notification(m.user, 'announcement', target=order,
                          data={'title': 'Order update', 'message': template.format(number=order.order_number),
                                'url': f"/orders/{order.pk}/detail"})

"""
order/sourcing.py
Order-side rules for items the seller gets after the order (imported or made
to order, see product/sourcing.py):

- At order time the line copies the sourcing window and gets `promised_by`.
- The seller reports progress: ordered → in transit → arrived. The buyer sees
  each step on the order page and gets a notification.
- The buyer may cancel for a full refund while the seller has not ordered the
  item yet, or once `promised_by` + GRACE_DAYS has passed with nothing shipped.
- A daily task nudges sellers who haven't ordered and tells buyers when a late
  line becomes cancellable.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from product import sourcing as product_sourcing

from .models import OrderProduct, Refund

logger = logging.getLogger(__name__)

STEPS = ['awaiting', 'ordered', 'in_transit', 'arrived']
# Days after promised_by before the buyer may cancel a late line.
GRACE_DAYS = 3
# Days a seller has to order the item from their supplier before reminders start.
ORDER_WITHIN_DAYS = 3

OPEN_LINE_STATUSES = ('pending', 'processing')


class SourcingError(Exception):
    pass


# ── Order creation ────────────────────────────────────────────────────────────

def snapshot(product, variant, delivery_option=None, delivery_option_id=None, now=None):
    """OrderProduct kwargs for a new line: {} for an in-stock item."""
    low, high = product_sourcing.window(product, variant)
    if not high:
        return {}
    if delivery_option is None and delivery_option_id:
        from product.models import DeliveryOption
        delivery_option = DeliveryOption.objects.filter(pk=delivery_option_id).only('max_days').first()
    courier_max = delivery_option.max_days if delivery_option else 0
    today = (now or timezone.now()).date()
    return {
        'sourcing_status': 'awaiting',
        'sourcing_min_days': low,
        'sourcing_max_days': high,
        'promised_by': today + timedelta(days=high + courier_max),
    }


def cart_needs_ack(cart_items):
    """True when any item must be sourced, so the buyer has to accept the wait."""
    return any(product_sourcing.window(i.product, i.variant)[1] for i in cart_items)


# ── Buyer ─────────────────────────────────────────────────────────────────────

def cancel_reason(line, today=None):
    """Why the buyer may cancel this line now, or None when they can't."""
    if not line.sourcing_status or line.status not in OPEN_LINE_STATUSES:
        return None
    if line.sourcing_status == 'awaiting':
        return 'not_ordered'
    today = today or timezone.localdate()
    if line.promised_by and today > line.promised_by + timedelta(days=GRACE_DAYS):
        return 'late'
    return None


def cancel_line(line, user):
    """Cancel a sourced line for the buyer. Raises SourcingError when not allowed."""
    from .fulfilment import status_from_lines

    with transaction.atomic():
        line = OrderProduct.objects.select_for_update(of=('self',)).select_related('order', 'product__vendor').get(pk=line.pk)
        order = line.order
        if order.user_id != user.pk:
            raise SourcingError("Order not found.")
        reason = cancel_reason(line)
        if reason is None:
            raise SourcingError("This item can't be cancelled any more.")
        if line.shipments.exclude(status='canceled').exists():
            raise SourcingError("This item has already been handed to the courier.")

        line.status = 'canceled'
        line.refund_reason = 'Late delivery' if reason == 'late' else 'Cancelled before the seller ordered it'
        line.save(update_fields=['status', 'refund_reason', 'date_updated'])
        _restock(line)
        if order.payment_method != 'cash_on_delivery':
            # Refunds are paid by Negromart staff (admin → Refunds).
            Refund.objects.create(order_product=line, amount=float(line.amount), reason=line.refund_reason)
        order.status = status_from_lines(order)
        order.save(update_fields=['status', 'date_updated'])

    _notify_cancelled(line)
    return line


def _restock(line):
    from product.models import Product, Variants
    if line.variant_id:
        Variants.objects.filter(pk=line.variant_id).update(quantity=F('quantity') + line.quantity)
    elif line.product_id:
        Product.objects.filter(pk=line.product_id).update(total_quantity=F('total_quantity') + line.quantity)


def _notify_cancelled(line):
    from notification.utils import send_notification
    order = line.order
    title = line.product.title if line.product else 'An item'
    paid = order.payment_method != 'cash_on_delivery'
    if order.user:
        send_notification(
            recipient=order.user, verb='customer_order_cancelled', target=order,
            data={
                'order_number': order.order_number,
                'message': f"{title} was cancelled."
                           + (f" Your refund of GHS {line.amount:,.2f} is on its way." if paid else ""),
                'url': f"/dashboard/order-history/{order.id}/",
            },
        )
    vendor = line.product.vendor if line.product else None
    if vendor and vendor.user_id:
        send_notification(
            recipient=vendor.user, verb='customer_order_cancelled', target=order,
            data={
                'order_number': order.order_number,
                'message': f"The customer cancelled {title} on order #{order.order_number} ({line.refund_reason.lower()}).",
                'url': f"/orders/{order.id}/detail/",
            },
        )


# ── Seller ────────────────────────────────────────────────────────────────────

def advance(order, vendor, new_status):
    """
    Move this store's open sourced lines in `order` to `new_status`. Steps only
    go forward; lines already at or past it are left alone. Returns the count.
    """
    if new_status not in STEPS[1:]:
        raise SourcingError("Unknown step.")
    earlier = STEPS[:STEPS.index(new_status)]
    now = timezone.now()
    updated = OrderProduct.objects.filter(
        order=order, product__vendor=vendor, status__in=OPEN_LINE_STATUSES,
        sourcing_status__in=earlier,
    ).update(sourcing_status=new_status, sourcing_updated_at=now)
    if not updated:
        raise SourcingError("Nothing to update: these items are already at this step or later.")

    if order.status == 'pending':
        order.status = 'processing'
        order.save(update_fields=['status', 'date_updated'])

    label = dict(OrderProduct.SOURCING_STATUS_CHOICES)[new_status]
    if order.user:
        from notification.utils import send_notification
        send_notification(
            recipient=order.user, verb='customer_tracking_update', target=order,
            data={
                'order_number': order.order_number,
                'vendor_name': vendor.name,
                'message': f"Order #{order.order_number}: {label.lower()}.",
                'url': f"/dashboard/order-history/{order.id}/",
            },
        )
    from .fulfilment import broadcast_tracking
    transaction.on_commit(lambda: broadcast_tracking(order.id))
    return updated


# ── Daily checks ──────────────────────────────────────────────────────────────

def remind_sellers(now=None):
    """One notification per store with sourced lines it hasn't ordered after ORDER_WITHIN_DAYS."""
    from notification.utils import send_notification
    from vendor.models import Vendor

    cutoff = (now or timezone.now()) - timedelta(days=ORDER_WITHIN_DAYS)
    counts = (
        OrderProduct.objects
        .filter(sourcing_status='awaiting', status__in=OPEN_LINE_STATUSES,
                order__is_ordered=True, date_created__lt=cutoff)
        .values('product__vendor').annotate(n=Count('id'))
    )
    stores = Vendor.objects.in_bulk([c['product__vendor'] for c in counts if c['product__vendor']])
    notified = 0
    for row in counts:
        vendor = stores.get(row['product__vendor'])
        if not vendor or not vendor.user_id:
            continue
        n = row['n']
        send_notification(
            recipient=vendor.user, verb='vendor_orders_late',
            data={
                'count': n,
                'message': f"{n} imported or made-to-order {'item has' if n == 1 else 'items have'} not been "
                           f"ordered yet. Order {'it' if n == 1 else 'them'} and mark the order as \"Ordered\" "
                           "so the customer can follow along.",
                'url': '/orders?filter=to_ship',
            },
        )
        notified += 1
    return notified


def notify_overdue(today=None):
    """Tell buyers, once per line, that a late sourced item can now be cancelled."""
    from notification.utils import send_notification

    today = today or timezone.localdate()
    lines = (
        OrderProduct.objects
        .filter(~Q(sourcing_status=''), status__in=OPEN_LINE_STATUSES, overdue_notified_at__isnull=True,
                promised_by__lt=today - timedelta(days=GRACE_DAYS))
        .select_related('order__user', 'product')
    )
    notified = []
    for line in lines.iterator():
        order = line.order
        if order.user:
            title = line.product.title if line.product else 'An item'
            send_notification(
                recipient=order.user, verb='customer_tracking_update', target=order,
                data={
                    'order_number': order.order_number,
                    'message': f"{title} is late. You can keep waiting, or cancel it for a full refund.",
                    'url': f"/dashboard/order-history/{order.id}/",
                },
            )
        notified.append(line.pk)
    OrderProduct.objects.filter(pk__in=notified).update(overdue_notified_at=timezone.now())
    return len(notified)


# ── API shape ─────────────────────────────────────────────────────────────────

def line_info(line):
    """A line's sourcing for buyer and seller views, or None for an in-stock item."""
    if not line.sourcing_status:
        return None
    labels = dict(OrderProduct.SOURCING_STATUS_CHOICES)
    return {
        'status': line.sourcing_status,
        'label': labels[line.sourcing_status],
        'steps': [{'key': k, 'label': labels[k]} for k in STEPS],
        'min_days': line.sourcing_min_days,
        'max_days': line.sourcing_max_days,
        'promised_by': line.promised_by.isoformat() if line.promised_by else None,
        'updated_at': line.sourcing_updated_at.isoformat() if line.sourcing_updated_at else None,
        'can_cancel': cancel_reason(line) is not None,
        'cancel_reason': cancel_reason(line),
    }

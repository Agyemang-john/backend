"""
order/sourcing.py
Order-side rules for items the seller gets after the order (imported or made
to order, see product/sourcing.py):

- At order time the line copies the sourcing window and gets `promised_by`.
- The seller reports progress: ordered → in transit → arrived. The buyer sees
  each step on the order page and gets a notification.
- The buyer may cancel for a full refund while the seller has not ordered the
  item yet, or once `promised_by` + GRACE_DAYS has passed with nothing shipped.
- Each line has dated deadlines (order it, have it in store, hand it to the
  courier, promised to the buyer). `deadlines()` works them out, the seller's
  "Imported orders" page lists every open line by urgency, and a daily digest
  (in-app + email) reminds the store about anything overdue or due soon, so a
  six-week order is not forgotten.
- The daily task also tells buyers when a late line becomes cancellable.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from product import sourcing as product_sourcing

from .models import OrderProduct, Refund

logger = logging.getLogger(__name__)

STEPS = ['awaiting', 'ordered', 'in_transit', 'arrived']
# Days after promised_by before the buyer may cancel a late line.
GRACE_DAYS = 3
# Days a seller has to order the item from their supplier before reminders start.
ORDER_WITHIN_DAYS = 3
# A deadline this close counts as "due soon" on the seller's page and digest.
DUE_SOON_DAYS = 3
# Same for the date promised to the buyer, which needs a little more warning.
PROMISE_SOON_DAYS = 5

# Field holding the date each step was reported.
STEP_DATE_FIELDS = {
    'ordered': 'sourcing_ordered_at',
    'in_transit': 'sourcing_in_transit_at',
    'arrived': 'sourcing_arrived_at',
}

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
    Every step passed is dated now unless it was reported before, so skipping
    straight to "arrived" still leaves a full timeline.
    """
    if new_status not in STEPS[1:]:
        raise SourcingError("Unknown step.")
    earlier = STEPS[:STEPS.index(new_status)]
    now = timezone.now()
    lines = list(OrderProduct.objects.select_for_update().filter(
        order=order, product__vendor=vendor, status__in=OPEN_LINE_STATUSES,
        sourcing_status__in=earlier,
    ))
    if not lines:
        raise SourcingError("Nothing to update: these items are already at this step or later.")
    passed = STEPS[1:STEPS.index(new_status) + 1]
    for line in lines:
        line.sourcing_status = new_status
        line.sourcing_updated_at = now
        line.sourcing_alert_level = ''  # new step, new deadline
        for step in passed:
            field = STEP_DATE_FIELDS[step]
            if getattr(line, field) is None:
                setattr(line, field, now)
    OrderProduct.objects.bulk_update(
        lines, ['sourcing_status', 'sourcing_updated_at', 'sourcing_alert_level', *STEP_DATE_FIELDS.values()])
    updated = len(lines)

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


def set_supplier_details(order, vendor, data):
    """
    Save the supplier's arrival date and/or tracking number on this store's
    open sourced lines in `order`. Only keys present in `data` change.
    """
    from datetime import date

    fields = {}
    if 'supplier_eta' in data:
        raw = data.get('supplier_eta')
        if raw in (None, ''):
            fields['supplier_eta'] = None
        else:
            try:
                eta = date.fromisoformat(str(raw))
            except ValueError:
                raise SourcingError("Enter the supplier's date as YYYY-MM-DD.")
            if eta < timezone.localdate():
                raise SourcingError("The supplier's arrival date can't be in the past.")
            fields['supplier_eta'] = eta
    if 'supplier_tracking' in data:
        fields['supplier_tracking'] = str(data.get('supplier_tracking') or '').strip()[:120]
    if not fields:
        raise SourcingError("Nothing to update.")
    updated = OrderProduct.objects.filter(
        order=order, product__vendor=vendor, status__in=OPEN_LINE_STATUSES,
    ).exclude(sourcing_status='').update(**fields)
    if not updated:
        raise SourcingError("This order has no imported items left to update.")
    return updated


# ── Deadlines ─────────────────────────────────────────────────────────────────
# Derived from the order date and the window copied at checkout, so they never
# move when the seller edits the product. ship_by matches order/fulfilment.py.

# What the seller does next at each step, and which deadline it answers to.
NEXT_STEP = {
    'awaiting': ('order_by', "Order it from your supplier"),
    'ordered': ('arrive_by', "Get it into your store"),
    'in_transit': ('arrive_by', "Get it into your store"),
    'arrived': ('ship_by', "Hand it to the courier"),
}

# Worst first. Stored on the line once the store has been emailed about it.
HEALTH_LEVELS = ['late', 'overdue', 'due_soon', 'on_track']


def deadlines(line, handling_days=None):
    """Every date that matters for a sourced line."""
    placed = timezone.localdate(line.date_created)
    if handling_days is None:
        vendor = line.product.vendor if line.product else None
        handling_days = vendor.handling_days if vendor else 0
    arrive_by = placed + timedelta(days=line.sourcing_max_days)
    return {
        'placed': placed,
        'order_by': placed + timedelta(days=ORDER_WITHIN_DAYS),
        'arrive_by': arrive_by,
        'ship_by': arrive_by + timedelta(days=handling_days),
        'promised_by': line.promised_by,
    }


def is_shipped(line):
    return any(s.status != 'canceled' for s in line.shipments.all())


def health(line, today=None, dates=None, shipped=None):
    """
    'late'     past the date promised to the buyer
    'overdue'  past the deadline of the current step
    'due_soon' that deadline, or the promise, is close
    'on_track' otherwise. None once the line is with the courier or closed.
    """
    if not line.sourcing_status or line.status not in OPEN_LINE_STATUSES:
        return None
    if is_shipped(line) if shipped is None else shipped:
        return None
    today = today or timezone.localdate()
    dates = dates or deadlines(line)
    key = NEXT_STEP[line.sourcing_status][0]
    due = dates[key]
    promised = dates['promised_by']
    if promised and today > promised:
        return 'late'
    if today > due:
        return 'overdue'
    # The order-it window is only ORDER_WITHIN_DAYS long, so it would always
    # read "due soon"; it only counts once missed.
    step_soon = key != 'order_by' and (due - today).days <= DUE_SOON_DAYS
    if step_soon or (promised and (promised - today).days <= PROMISE_SOON_DAYS):
        return 'due_soon'
    return 'on_track'


def open_lines(vendor):
    """This store's sourced lines that have not been handed to a courier yet."""
    shipped = OrderProduct.shipments.through.objects.filter(
        orderproduct_id=OuterRef('pk'),
    ).exclude(shipment__status='canceled')
    return (
        OrderProduct.objects
        .filter(product__vendor=vendor, order__is_ordered=True, status__in=OPEN_LINE_STATUSES)
        .exclude(sourcing_status='')
        .exclude(Exists(shipped))
        .select_related('order__user', 'order__address', 'product__vendor', 'variant__size', 'variant__color')
        .prefetch_related('shipments')
    )


def attention(vendor, today=None):
    """[(line, dates, health)] for this store's open sourced lines, most urgent first."""
    today = today or timezone.localdate()
    rows = []
    for line in open_lines(vendor):
        dates = deadlines(line, vendor.handling_days)
        rows.append((line, dates, health(line, today, dates, shipped=False)))
    rows.sort(key=lambda r: (HEALTH_LEVELS.index(r[2]), dates_due(r[0], r[1])))
    return rows


def dates_due(line, dates):
    return dates[NEXT_STEP[line.sourcing_status][0]]


def health_counts(rows):
    counts = dict.fromkeys(HEALTH_LEVELS, 0)
    for _, _, h in rows:
        counts[h] += 1
    return counts


# ── Daily checks ──────────────────────────────────────────────────────────────

def _item_name(line):
    return line.product.title if line.product else 'An item'


def _digest_line(line, dates, level, today):
    action = NEXT_STEP[line.sourcing_status][1]
    due = dates_due(line, dates)
    if level == 'late':
        when = f"promised to the customer by {dates['promised_by']:%b %d}, now {(today - dates['promised_by']).days} days late"
    elif level == 'overdue':
        when = f"was due {due:%b %d} ({(today - due).days} days ago)"
    else:
        when = f"by {due:%b %d}"
    return {
        'order_number': line.order.order_number,
        'order_id': line.order_id,
        'item': _item_name(line),
        'action': action,
        'when': when,
        'level': level,
        'promised_by': dates['promised_by'],
        'days_left': (dates['promised_by'] - today).days if dates['promised_by'] else None,
    }


def remind_sellers(today=None):
    """
    One digest per store with imported / made-to-order lines that are late,
    overdue or due soon. The in-app notification goes out every day while
    anything needs action; the email only when a line gets worse since the
    store was last emailed about it (sourcing_alert_level), so it is not spam.
    """
    from notification.utils import send_notification
    from vendor.models import Vendor
    from vendor.tasks import send_sourcing_digest_email

    today = today or timezone.localdate()
    store_ids = (OrderProduct.objects
                 .filter(order__is_ordered=True, status__in=OPEN_LINE_STATUSES)
                 .exclude(sourcing_status='')
                 .values_list('product__vendor', flat=True).distinct())
    notified = 0
    for vendor in Vendor.objects.filter(pk__in=store_ids).select_related('user'):
        rows = [r for r in attention(vendor, today) if r[2] != 'on_track']
        if not rows or not vendor.user_id:
            continue
        counts = health_counts(rows)
        parts = [f"{counts[k]} {label}" for k, label in
                 (('late', 'late'), ('overdue', 'overdue'), ('due_soon', 'due soon')) if counts[k]]
        first_line, first_dates, first_level = rows[0]
        first = _digest_line(first_line, first_dates, first_level, today)
        send_notification(
            recipient=vendor.user, verb='vendor_sourcing_due',
            data={
                'count': len(rows),
                'message': f"Imported orders: {', '.join(parts)}. Next: {first['action'].lower()} "
                           f"({first['item']}, #{first['order_number']}) {first['when']}.",
                'url': '/imported-orders',
            },
        )
        notified += 1

        worse = [line for line, _, level in rows
                 if not line.sourcing_alert_level
                 or HEALTH_LEVELS.index(level) < HEALTH_LEVELS.index(line.sourcing_alert_level)]
        if worse:
            levels = {line.pk: level for line, _, level in rows}
            for line in worse:
                line.sourcing_alert_level = levels[line.pk]
            OrderProduct.objects.bulk_update(worse, ['sourcing_alert_level'])
            items = [_digest_line(line, dates, level, today) for line, dates, level in rows]
            transaction.on_commit(lambda vid=vendor.pk, items=items, parts=parts:
                                  send_sourcing_digest_email.delay(vid, _jsonable(items), ', '.join(parts)))
    return notified


def _jsonable(items):
    return [{**i, 'promised_by': i['promised_by'].isoformat() if i['promised_by'] else None} for i in items]


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

def _iso(value):
    return value.isoformat() if value else None


def line_info(line, seller=False, today=None, dates=None, shipped=None):
    """
    A line's sourcing for buyer and seller views, or None for an in-stock item.
    `seller=True` adds the store's deadlines, urgency and supplier details,
    which the buyer never sees.
    """
    if not line.sourcing_status:
        return None
    labels = dict(OrderProduct.SOURCING_STATUS_CHOICES)
    today = today or timezone.localdate()
    placed = timezone.localdate(line.date_created)
    step_dates = {'awaiting': line.date_created,
                  **{k: getattr(line, f) for k, f in STEP_DATE_FIELDS.items()}}
    info = {
        'status': line.sourcing_status,
        'label': labels[line.sourcing_status],
        'steps': [{'key': k, 'label': labels[k], 'date': _iso(step_dates[k])} for k in STEPS],
        'min_days': line.sourcing_min_days,
        'max_days': line.sourcing_max_days,
        'placed_on': placed.isoformat(),
        'promised_by': _iso(line.promised_by),
        'days_left': (line.promised_by - today).days if line.promised_by else None,
        'updated_at': _iso(line.sourcing_updated_at),
        'can_cancel': cancel_reason(line) is not None,
        'cancel_reason': cancel_reason(line),
    }
    if not seller:
        return info

    dates = dates or deadlines(line)
    level = health(line, today, dates, shipped=shipped)
    key, action = NEXT_STEP[line.sourcing_status]
    due = dates[key]
    total = (line.promised_by - placed).days if line.promised_by else None
    info.update({
        'deadlines': {k: _iso(v) for k, v in dates.items()},
        'health': level,
        'next_action': action if level else None,
        'next_due': _iso(due) if level else None,
        'next_due_in': (due - today).days if level else None,
        'day_number': (today - placed).days + 1,
        'total_days': total,
        'supplier_eta': _iso(line.supplier_eta),
        'supplier_tracking': line.supplier_tracking,
        # The supplier's date leaves no room to ship before the promise.
        'supplier_eta_late': bool(line.supplier_eta and line.supplier_eta > dates['arrive_by']),
    })
    return info

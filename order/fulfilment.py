"""
order/fulfilment.py
Shipment and order-status rules shared by every path that moves a parcel:
the seller dashboard, Negromart's delivery team (admin), and courier webhooks.

Keeping them here means "delivered" always has the same consequences
(order status, line dates, seller earnings), whoever reports it.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Order, OrderProduct, Shipment, TrackingEvent

logger = logging.getLogger(__name__)

# Tracking event → shipment status.
EVENT_TO_SHIPMENT_STATUS = {
    'in_transit': 'in_transit',
    'out_for_delivery': 'out_for_delivery',
    'delivered': 'delivered',
    'failed_attempt': 'failed',
    'returned_to_sender': 'returned',
}

# Order lines a seller still has to dispatch.
AWAITING_DISPATCH_ORDER_STATUSES = ('pending', 'processing')


# ── Order status ──────────────────────────────────────────────────────────────

def recompute_order_status(order):
    """
    Derive Order.status from its shipments.

    - No shipments                 → unchanged
    - All vendors' shipments delivered → 'delivered'
    - Some delivered               → 'partially_delivered'
    - Any in transit / labelled    → 'shipped'
    - Otherwise                    → 'processing'
    Canceled orders are left alone.
    """
    if order.status == 'canceled':
        return

    shipments = list(order.shipments.all())
    if not shipments:
        return

    statuses = [s.status for s in shipments]
    total_vendors = order.vendors.count()

    if all(s == 'delivered' for s in statuses) and len(statuses) == total_vendors:
        new_status = 'delivered'
    elif any(s == 'delivered' for s in statuses):
        new_status = 'partially_delivered'
    elif any(s in ('out_for_delivery', 'in_transit', 'label_created') for s in statuses):
        new_status = 'shipped'
    else:
        new_status = 'processing'

    if order.status != new_status:
        order.status = new_status
        order.save(update_fields=['status'])


# ── Delivery ──────────────────────────────────────────────────────────────────

def on_shipment_delivered(shipment):
    """
    Everything that follows a delivery. Safe to call more than once.
    Line dates are stamped, and the seller's earnings are posted to the
    ledger (which also starts the return-window/payout hold clock).
    """
    now = timezone.now()
    if not shipment.delivered_at:
        Shipment.objects.filter(pk=shipment.pk, delivered_at__isnull=True).update(delivered_at=now)
        shipment.delivered_at = now

    shipment.items.exclude(status='canceled').filter(delivered_date__isnull=True).update(
        status='delivered', delivered_date=shipment.delivered_at,
    )

    from payments.ledger import post_shipment_earnings
    # After commit, so a rollback of the caller never leaves money behind.
    transaction.on_commit(lambda: post_shipment_earnings(shipment.pk))


def record_tracking_event(shipment, *, status, description, event_date,
                          location='', city='', country=''):
    """Store a tracking event and apply its consequences. Returns the event."""
    event = TrackingEvent.objects.create(
        shipment=shipment, status=status, description=description,
        location=location, city=city, country=country, event_date=event_date,
    )

    new_status = EVENT_TO_SHIPMENT_STATUS.get(status)
    if new_status and shipment.status != new_status:
        shipment.status = new_status
        shipment.save(update_fields=['status', 'updated_at'])

    if status == 'delivered':
        on_shipment_delivered(shipment)

    if status in ('delivered', 'returned_to_sender', 'failed_attempt'):
        recompute_order_status(shipment.order)
    return event


# ── Ship-by deadlines ─────────────────────────────────────────────────────────
# A seller has `vendor.handling_days` from the order date to hand the parcel
# over. Derived, not stored: it stays correct if the seller changes the
# setting, and it costs no write on the order-creation hot path.

def ship_by_for(order, vendor):
    return order.date_created + timedelta(days=vendor.handling_days)


def awaiting_dispatch_orders(vendor):
    """Paid orders containing this store's items with no shipment yet."""
    return (
        Order.objects
        .filter(vendors=vendor, is_ordered=True, status__in=AWAITING_DISPATCH_ORDER_STATUSES)
        .exclude(shipments__vendor=vendor)
        .distinct()
    )


def late_orders(vendor, now=None):
    cutoff = (now or timezone.now()) - timedelta(days=vendor.handling_days)
    return awaiting_dispatch_orders(vendor).filter(date_created__lt=cutoff)


def vendor_lines(order, vendor):
    return OrderProduct.objects.filter(order=order, product__vendor=vendor)


# ── Booking, cancelling and live updates ──────────────────────────────────────
# One path for every caller (seller dashboard, admin, webhooks), so a courier
# integration is exercised the same way whoever triggers it. Before this the
# admin "create shipment" never called the provider at all, and cancelling an
# order left the parcel booked with the courier.

TERMINAL_SHIPMENT_STATUSES = ('delivered', 'canceled', 'returned')


def book_shipment(order, vendor, items, provider, **fields):
    """
    Create a shipment for `vendor`'s `items` and hand it to `provider`.
    Raises DeliveryProviderError (with nothing saved) if the provider refuses.
    """
    from .delivery import DeliveryProviderError

    fields.setdefault('fulfilled_by', provider.fulfilled_by)
    shipment = Shipment.objects.create(order=order, vendor=vendor, provider=provider.code, **fields)
    shipment.items.set(items)
    try:
        booking = provider.book(shipment)
    except DeliveryProviderError:
        shipment.delete()
        raise
    except Exception as exc:
        # A provider bug or network library error must not leave a shipment
        # that looks booked but that no courier knows about.
        shipment.delete()
        logger.exception("delivery: %s failed to book shipment for order %s", provider.code, order.pk)
        raise DeliveryProviderError(f"{provider.name} could not book this delivery.") from exc

    shipment.tracking_number = shipment.tracking_number or booking.tracking_number
    shipment.tracking_url = shipment.tracking_url or booking.tracking_url
    shipment.carrier = shipment.carrier or booking.carrier_name or provider.name
    shipment.external_reference = booking.external_reference
    shipment.label_url = booking.label_url
    shipment.provider_data = booking.raw
    shipment.save(update_fields=['tracking_number', 'tracking_url', 'carrier', 'external_reference',
                                 'label_url', 'provider_data', 'updated_at'])
    return shipment


def cancel_shipment(shipment):
    """
    Cancel with the courier first, then locally. Raises DeliveryProviderError
    if the courier refuses (e.g. already out for delivery) and changes nothing.
    """
    from .delivery import get_provider

    if shipment.status in TERMINAL_SHIPMENT_STATUSES:
        return shipment
    get_provider(shipment.provider).cancel(shipment)
    shipment.status = 'canceled'
    shipment.save(update_fields=['status', 'updated_at'])
    return shipment


def cancel_open_shipments(order, vendor=None):
    """
    Cancel every shipment of `order` that is still moving, or only `vendor`'s
    when a store cancels its part of a shared order. Stops at the first refusal.
    """
    shipments = order.shipments.exclude(status__in=TERMINAL_SHIPMENT_STATUSES)
    if vendor is not None:
        shipments = shipments.filter(vendor=vendor)
    for shipment in shipments:
        cancel_shipment(shipment)


def broadcast_tracking(order_id):
    """Push the latest tracking snapshot to the customer's WebSocket group."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        from .serializers import OrderTrackingSerializer

        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        order = Order.objects.prefetch_related(
            'vendors',
            'shipments__tracking_events',
            'shipments__items__product',
            'shipments__vendor',
        ).get(id=order_id)
        async_to_sync(channel_layer.group_send)(
            f'order_tracking_{order_id}',
            {'type': 'tracking_update', 'data': OrderTrackingSerializer(order).data},
        )
    except Exception as exc:
        logger.warning("Broadcast tracking failed for order %s: %s", order_id, exc)


def notify_tracking_event(shipment, event):
    """Tell the buyer about a tracking event, whoever reported it."""
    order = shipment.order
    if not order.user:
        return
    from notification.utils import send_notification
    send_notification(
        recipient=order.user,
        verb="customer_order_delivered" if event.status == "delivered" else "customer_tracking_update",
        target=order,
        data={
            "order_number": order.order_number,
            "vendor_name": shipment.vendor.name if shipment.vendor else "",
            "event_status": event.get_status_display(),
            "description": event.description,
            "location": ", ".join(filter(None, [event.city, event.country])),
            "message": event.description,
            "url": f"/dashboard/order-history/{order.id}/",
        },
    )

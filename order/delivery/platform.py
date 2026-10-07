"""
order/delivery/platform.py
Negromart's own delivery service (the only provider in use today).

Booking needs no external call: the shipment gets a Negromart tracking
number and the delivery team progresses it from the admin, the delivery
dashboard, or the seller/rider tracking-event endpoint.
"""

from .base import DeliveryProvider, ShipmentBooking


class PlatformDeliveryProvider(DeliveryProvider):
    code = 'platform'
    name = 'Negromart Delivery'
    fulfilled_by = 'platform'
    supports_tracking_sync = False

    def book(self, shipment):
        return ShipmentBooking(
            tracking_number=shipment.tracking_number or f"NM{shipment.shipment_id.replace('SH-', '')}",
            carrier_name=self.name,
        )

    def cancel(self, shipment):
        # Nothing external to cancel; the status change is enough.
        return None

    def book_return_pickup(self, return_request):
        # The delivery team schedules pickups from the admin for now.
        return None

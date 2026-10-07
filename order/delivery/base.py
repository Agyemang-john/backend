"""
order/delivery/base.py
The contract every delivery provider implements.

Today Negromart delivers everything itself (PlatformDeliveryProvider). An
external courier is added by subclassing DeliveryProvider, implementing the
calls it supports, and listing it in settings.DELIVERY_PROVIDERS. Views and
tasks only ever talk to this interface, so nothing else changes.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ShipmentBooking:
    """What a provider returns after accepting a shipment."""
    tracking_number: str = ''
    tracking_url: str = ''
    external_reference: str = ''
    label_url: str = ''
    carrier_name: str = ''
    raw: dict = field(default_factory=dict)


@dataclass
class TrackingUpdate:
    """One status change reported by a provider (webhook or polling)."""
    shipment_lookup: str          # our shipment_id, or the provider's external_reference
    status: str                   # one of TrackingEvent.STATUS_CHOICES keys
    description: str
    event_date: datetime
    location: str = ''
    city: str = ''
    country: str = ''


class DeliveryProviderError(Exception):
    """The provider refused or failed; message is safe to show to staff."""


class DeliveryProvider:
    #: Key used in settings.DELIVERY_PROVIDERS and stored on Shipment.provider.
    code = ''
    #: Human name shown to sellers and customers.
    name = ''
    #: 'platform' | 'seller' | 'carrier'. Decides who earns the delivery fee
    #: (see payments/ledger.py): only seller-fulfilled shipments pay it to the seller.
    fulfilled_by = 'carrier'
    #: Whether the provider pushes or can be polled for tracking events.
    supports_tracking_sync = False

    def book(self, shipment) -> ShipmentBooking:
        """Hand a new shipment to the provider (create the delivery job)."""
        raise NotImplementedError

    def cancel(self, shipment) -> None:
        """Cancel a booked delivery, if the provider allows it."""
        raise DeliveryProviderError(f"{self.name} does not support cancelling deliveries.")

    def book_return_pickup(self, return_request):
        """Collect a returned item from the customer. Optional."""
        raise DeliveryProviderError(f"{self.name} does not support return pickups.")

    def parse_webhook(self, request) -> list[TrackingUpdate]:
        """Verify and translate a webhook call into tracking updates."""
        raise DeliveryProviderError(f"{self.name} does not send webhooks.")

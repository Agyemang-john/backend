"""
order/delivery: pluggable delivery providers.

    from order.delivery import get_provider, default_provider
    provider = get_provider(shipment.provider)
    booking = provider.book(shipment)

Configure in settings (dotted paths; first-party default shown):

    DELIVERY_PROVIDERS = {
        'platform': 'order.delivery.platform.PlatformDeliveryProvider',
        # 'some_courier': 'order.delivery.some_courier.SomeCourierProvider',
    }
    DEFAULT_DELIVERY_PROVIDER = 'platform'
    INTERNATIONAL_DELIVERY_PROVIDER = ''   # blank: same as the default

New shipments pick their provider with provider_for(), so domestic and
international parcels can go to different couriers.
"""

from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string

from .base import DeliveryProvider, DeliveryProviderError, ShipmentBooking, TrackingUpdate

_BUILTIN = {'platform': 'order.delivery.platform.PlatformDeliveryProvider'}


def _registry():
    return {**_BUILTIN, **getattr(settings, 'DELIVERY_PROVIDERS', {})}


@lru_cache(maxsize=None)
def get_provider(code) -> DeliveryProvider:
    path = _registry().get(code)
    if not path:
        raise DeliveryProviderError(f"Unknown delivery provider '{code}'.")
    return import_string(path)()


def default_provider() -> DeliveryProvider:
    return get_provider(getattr(settings, 'DEFAULT_DELIVERY_PROVIDER', 'platform'))


def provider_for(*, is_international=False) -> DeliveryProvider:
    """The provider a new shipment should be booked with."""
    if is_international in (True, 'true', 'True', '1', 1):
        code = getattr(settings, 'INTERNATIONAL_DELIVERY_PROVIDER', '')
        if code:
            return get_provider(code)
    return default_provider()


def available_providers():
    return [get_provider(code) for code in _registry()]


__all__ = [
    'DeliveryProvider', 'DeliveryProviderError', 'ShipmentBooking', 'TrackingUpdate',
    'get_provider', 'default_provider', 'provider_for', 'available_providers',
]

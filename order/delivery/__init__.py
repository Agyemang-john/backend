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


def available_providers():
    return [get_provider(code) for code in _registry()]


__all__ = [
    'DeliveryProvider', 'DeliveryProviderError', 'ShipmentBooking', 'TrackingUpdate',
    'get_provider', 'default_provider', 'available_providers',
]

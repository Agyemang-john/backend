"""
order/delivery/raven.py
Raven, an external delivery platform, over its partner API (/api/v1).

Not active until listed in settings.DELIVERY_PROVIDERS and chosen as the
default (or international) provider, with RAVEN_API_URL / RAVEN_API_KEY /
RAVEN_WEBHOOK_SECRET set. Raven's webhook endpoint for us is
    <our host>/api/v1/order/delivery/webhooks/raven/
subscribed to `shipments.status_changed`.

Booking prices at the moment the seller hands over the parcel: Raven only
books a quote it issued, and its quotes last an hour, so a quote taken at
checkout has usually expired by dispatch. The fee the buyer paid at checkout
is Negromart's own price; the difference is Negromart's margin or cost.
"""

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .base import DeliveryProvider, DeliveryProviderError, ShipmentBooking, TrackingUpdate

logger = logging.getLogger(__name__)

SIGNATURE_TOLERANCE_SECONDS = 300

# Address/About rows default to these when nobody set a location. Sending them
# would price every such seller from central Accra, so they are treated as unset.
_PLACEHOLDER_COORDS = (5.5600, -0.2050)

# Raven shipment status -> (our TrackingEvent status, buyer-facing description).
# Anything unlisted is kept as 'info' with Raven's own status name, never dropped.
STATUS_MAP = {
    'CREATED': ('info', 'Delivery booked with Raven'),
    'PICKUP_SCHEDULED': ('info', 'Pickup scheduled from the seller'),
    'AWAITING_DOCUMENTS': ('info', 'Waiting for shipping documents'),
    'AWAITING_PRICE_CONFIRMATION': ('info', 'Waiting for price confirmation'),
    'BOOKING_FAILED': ('exception', 'The courier could not book this delivery'),
    'PICKED_UP': ('in_transit', 'Picked up from the seller'),
    'AT_HUB': ('in_transit', 'Arrived at a sorting hub'),
    'IN_TRANSIT': ('in_transit', 'In transit'),
    'EXPORT_CUSTOMS_PENDING': ('in_transit', 'Awaiting export customs'),
    'EXPORT_CUSTOMS_CLEARED': ('in_transit', 'Cleared export customs'),
    'HANDED_TO_PARTNER': ('in_transit', 'Handed to the international carrier'),
    'IN_TRANSIT_INTERNATIONAL': ('in_transit', 'In transit internationally'),
    'IMPORT_CUSTOMS_PENDING': ('in_transit', 'Awaiting import customs'),
    'IMPORT_CUSTOMS_CLEARED': ('in_transit', 'Cleared import customs'),
    'IMPORT_CUSTOMS_HELD': ('exception', 'Held at customs'),
    'DUTIES_PAYABLE': ('exception', 'Import duties are payable'),
    'CUSTOMS_REJECTED': ('exception', 'Rejected by customs'),
    'OUT_FOR_DELIVERY': ('out_for_delivery', 'Out for delivery'),
    'FAILED_ATTEMPT': ('failed_attempt', 'Delivery attempted, nobody available'),
    'RETURNING': ('exception', 'Being returned to the seller'),
    'RETURNED': ('returned_to_sender', 'Returned to the seller'),
    'DELIVERED': ('delivered', 'Delivered'),
    'CANCELLED': ('exception', 'Delivery cancelled by the courier'),
    'LOST': ('exception', 'Reported lost by the courier'),
    'DAMAGED': ('exception', 'Reported damaged by the courier'),
    'SEIZED': ('exception', 'Seized by the authorities'),
}


def _setting(name, default=''):
    return getattr(settings, name, default)


def _iso2(value, fallback='GH'):
    """Raven wants ISO-3166 alpha-2; Negromart stores codes and names both."""
    if not value:
        return fallback
    value = str(getattr(value, 'code', value) or '').strip()
    if len(value) == 2:
        return value.upper()
    from django_countries import countries
    return countries.by_name(value) or fallback


def _coords(lat, lng):
    if lat is None or lng is None or (round(lat, 4), round(lng, 4)) == _PLACEHOLDER_COORDS:
        return {}
    return {'latitude': str(round(Decimal(str(lat)), 7)), 'longitude': str(round(Decimal(str(lng)), 7))}


def _minor(amount):
    return int((Decimal(str(amount or 0)) * 100).quantize(Decimal('1')))


class RavenDeliveryProvider(DeliveryProvider):
    code = 'raven'
    name = 'Raven'
    fulfilled_by = 'carrier'
    supports_tracking_sync = True

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _request(self, method, path, *, json_body=None, params=None, idempotency_key=''):
        base = _setting('RAVEN_API_URL').rstrip('/')
        key = _setting('RAVEN_API_KEY')
        if not base or not key:
            raise DeliveryProviderError("Raven is not configured (RAVEN_API_URL / RAVEN_API_KEY).")
        headers = {'X-Api-Key': key, 'Accept': 'application/json'}
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        return requests.request(
            method, f"{base}/v1/{path}", json=json_body, params=params, headers=headers,
            timeout=_setting('RAVEN_TIMEOUT_SECONDS', 15),
        )

    @staticmethod
    def _error(response):
        try:
            body = response.json()
            detail = body.get('detail') or body.get('error') or response.text
        except ValueError:
            detail = response.text
        return DeliveryProviderError(f"Raven refused the request: {str(detail)[:300]}")

    # ── Payload ───────────────────────────────────────────────────────────────

    def _origin(self, vendor):
        about = getattr(vendor, 'about', None)
        return {
            'country': _iso2(getattr(vendor.shipping_from_country, 'code', None) or vendor.country),
            'line1': (about.address if about else '')[:200],
            'contact_name': vendor.name[:150],
            'contact_phone': vendor.contact or '',
            'contact_email': vendor.email or '',
            **(_coords(about.latitude, about.longitude) if about else {}),
        }

    def _destination(self, order):
        address = order.address
        if address is None:
            raise DeliveryProviderError("This order has no delivery address.")
        return {
            'country': _iso2(address.country),
            'line1': (address.address or '')[:200],
            'locality': address.town or '',
            'admin_area': address.region or '',
            'digital_address': address.gps_address or '',
            'contact_name': (address.full_name or '')[:150],
            'contact_phone': address.mobile or '',
            'contact_email': address.email or '',
            **_coords(address.latitude, address.longitude),
        }

    def _parcel(self, items):
        # Product.weight is kilograms. One parcel per shipment: one seller
        # packs one order's items together.
        grams = sum(int((item.product.weight or 0) * 1000) * item.quantity
                    for item in items if item.product)
        return {'weight_grams': max(grams, 1)}

    def _cod_minor(self, shipment, items):
        """Cash the rider collects: this seller's lines plus their delivery fee."""
        order = shipment.order
        if order.payment_method != 'cash_on_delivery':
            return None
        fee = order.calculate_vendor_delivery_fee(shipment.vendor)
        return _minor(sum(item.amount for item in items)) + _minor(getattr(fee, 'total', fee))

    def _shipment_body(self, shipment, quote_id, service_code):
        items = list(shipment.items.exclude(status='canceled').select_related('product'))
        if not items:
            raise DeliveryProviderError("This shipment has no items to deliver.")
        titles = ", ".join(item.product.title for item in items if item.product)
        body = {
            'origin': self._origin(shipment.vendor),
            'destination': self._destination(shipment.order),
            'parcels': [self._parcel(items)],
            'service_code': service_code,
            'quote_id': quote_id,
            'contents_description': (titles or f"Order {shipment.order.order_number}")[:200],
            'currency': 'GHS',
            # Our id, so Raven's webhooks can be matched to the shipment even
            # before the booking response that carries Raven's id is saved.
            'reference': shipment.shipment_id,
        }
        cod = self._cod_minor(shipment, items)
        if cod:
            body['cod_amount_minor'] = cod
        return body

    # ── Provider API ──────────────────────────────────────────────────────────

    def book(self, shipment):
        service_code = _setting('RAVEN_SERVICE_CODE', 'STANDARD')
        body = self._shipment_body(shipment, quote_id=None, service_code=service_code)

        rate = self._request('POST', 'rates', json_body={
            'origin': body['origin'], 'destination': body['destination'], 'parcels': body['parcels'],
            'currency': body['currency'], 'service_codes': [service_code],
            'cod_amount_minor': body.get('cod_amount_minor'),
        })
        if rate.status_code != 200:
            raise self._error(rate)
        offers = rate.json().get('offers') or []
        if not offers:
            refusals = rate.json().get('refusals') or []
            reason = refusals[0].get('detail') or refusals[0].get('reason') if refusals else ''
            raise DeliveryProviderError(f"Raven can't deliver this route with {service_code}. {reason}".strip())
        body['quote_id'] = offers[0]['quote_id']

        try:
            response = self._request('POST', 'shipments', json_body=body,
                                     idempotency_key=shipment.shipment_id)
        except requests.RequestException as exc:
            # A timeout doesn't say whether Raven created it. Ask, so we never
            # delete our side of a delivery Raven is about to collect.
            existing = self._find_by_reference(shipment.shipment_id)
            if existing:
                return self._booking(existing)
            raise DeliveryProviderError("Raven could not be reached. Try again.") from exc
        if response.status_code >= 500:
            existing = self._find_by_reference(shipment.shipment_id)
            if existing:
                return self._booking(existing)
        if response.status_code not in (200, 201):
            raise self._error(response)
        return self._booking(response.json())

    def _find_by_reference(self, reference):
        try:
            response = self._request('GET', 'shipments', params={'reference': reference, 'limit': 1})
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        found = response.json().get('shipments') or []
        return found[0] if found else None

    def _booking(self, data):
        tracking = data.get('tracking_number', '')
        template = _setting('RAVEN_TRACKING_URL', '')
        return ShipmentBooking(
            tracking_number=tracking,
            tracking_url=template.format(tracking_number=tracking) if template and tracking else '',
            external_reference=str(data.get('shipment_id', '')),
            carrier_name=self.name,
            raw=data,
        )

    def cancel(self, shipment):
        if not shipment.external_reference:
            return None
        try:
            response = self._request('POST', f"shipments/{shipment.external_reference}/cancel",
                                     json_body={'reason': 'CUSTOMER_REQUEST'})
        except requests.RequestException as exc:
            raise DeliveryProviderError("Raven could not be reached to cancel. Try again.") from exc
        if response.status_code != 200:
            raise self._error(response)
        return None

    def parse_webhook(self, request):
        secret = _setting('RAVEN_WEBHOOK_SECRET')
        if not secret:
            raise DeliveryProviderError("Raven webhooks are not configured.")
        body = request.body
        signature = request.headers.get('X-Raven-Signature', '')
        try:
            timestamp = int(request.headers.get('X-Raven-Timestamp', ''))
        except ValueError:
            raise DeliveryProviderError("Missing webhook timestamp.")
        if abs(time.time() - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
            raise DeliveryProviderError("Webhook timestamp outside tolerance.")
        expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise DeliveryProviderError("Bad webhook signature.")

        try:
            payload = json.loads(body)
        except ValueError:
            raise DeliveryProviderError("Webhook body is not JSON.")
        if payload.get('type') != 'shipments.status_changed':
            return []

        data = payload.get('data') or {}
        raven_status = data.get('to', '')
        status, description = STATUS_MAP.get(raven_status, ('info', raven_status.replace('_', ' ').capitalize()))
        occurred = parse_datetime(data.get('occurred_at') or '') or timezone.now()
        return [TrackingUpdate(
            shipment_lookup=data.get('reference') or data.get('shipment_id', ''),
            status=status,
            description=description,
            event_date=occurred,
            event_id=str(payload.get('id', '')),
        )]

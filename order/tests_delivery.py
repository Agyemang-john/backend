"""
order/tests_delivery.py
External couriers through order/delivery/: provider routing, the shared
booking path, courier cancellation, webhook de-duplication, and the Raven
provider against a mocked Raven API.

Run with:  DB_HOST=localhost python manage.py test order.tests_delivery --keepdb
"""

import hashlib
import hmac
import json
import time
from unittest import mock

import requests
from django.test import override_settings
from rest_framework.test import APIClient

from address.models import Address
from order.delivery import get_provider, provider_for
from order.models import DeliveryWebhookEvent, Order, Shipment, TrackingEvent
from vendor.tests_operations import OpsTestCase, make_user, vendor_client

SECRET = 'whsec_test'
RAVEN_SETTINGS = {
    'DEFAULT_DELIVERY_PROVIDER': 'raven',
    'RAVEN_API_URL': 'https://raven.test/api',
    'RAVEN_API_KEY': 'rk_test_key',
    'RAVEN_WEBHOOK_SECRET': SECRET,
    'RAVEN_TRACKING_URL': 'https://raven.test/track/{tracking_number}',
}
RAVEN_ID = '7c1e3a52-5b1c-4c55-9f43-0f6c2d8f1a10'


def response(status, body):
    res = mock.Mock(status_code=status, text=json.dumps(body))
    res.json.return_value = body
    return res


def raven_shipment(reference):
    return {'shipment_id': RAVEN_ID, 'tracking_number': 'RVN7K2M9P4XA', 'status': 'CREATED',
            'reference': reference}


def signed(body, secret=SECRET, timestamp=None):
    timestamp = timestamp or int(time.time())
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {'HTTP_X_RAVEN_SIGNATURE': signature, 'HTTP_X_RAVEN_TIMESTAMP': str(timestamp)}


class DeliveryTestCase(OpsTestCase):

    def setUp(self):
        super().setUp()
        self.order.address = Address.objects.create(
            user=self.customer, full_name='Ama Mensah', country='Ghana', region='Ashanti', town='Kumasi',
            address='12 Adum Rd', mobile='+233240001111', email='ama@example.com',
        )
        self.order.save(update_fields=['address'])

    def book_via_seller(self):
        return vendor_client(self.owner).post(f'/api/v1/vendor/orders/{self.order.pk}/shipment/', {}, format='json')


class RoutingTests(DeliveryTestCase):

    @override_settings(DEFAULT_DELIVERY_PROVIDER='platform', INTERNATIONAL_DELIVERY_PROVIDER='raven')
    def test_international_parcels_can_use_a_different_courier(self):
        self.assertEqual(provider_for(is_international=False).code, 'platform')
        self.assertEqual(provider_for(is_international=True).code, 'raven')
        self.assertEqual(provider_for(is_international='true').code, 'raven')

    @override_settings(DEFAULT_DELIVERY_PROVIDER='platform', INTERNATIONAL_DELIVERY_PROVIDER='')
    def test_international_falls_back_to_the_default(self):
        self.assertEqual(provider_for(is_international=True).code, 'platform')

    def test_admin_created_shipments_are_booked_with_the_provider(self):
        """The admin path used to create the row without ever calling book()."""
        admin = make_user(9, is_staff=True)
        client = APIClient()
        client.force_authenticate(admin)
        res = client.post(f'/api/v1/order/admin/orders/{self.order.pk}/shipment/',
                          {'vendor_id': self.vendor.pk}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(Shipment.objects.get(order=self.order).tracking_number.startswith('NM'))


@override_settings(**RAVEN_SETTINGS)
class RavenBookingTests(DeliveryTestCase):

    def test_books_a_fresh_quote_with_our_id_as_reference_and_idempotency_key(self):
        calls = []

        def fake(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith('/v1/rates'):
                return response(200, {'offers': [{'quote_id': 'q-1', 'service_code': 'STANDARD'}], 'refusals': []})
            return response(201, raven_shipment(kwargs['json']['reference']))

        with mock.patch('order.delivery.raven.requests.request', side_effect=fake):
            res = self.book_via_seller()

        self.assertEqual(res.status_code, 201, res.data)
        shipment = Shipment.objects.get(order=self.order)
        self.assertEqual((shipment.provider, shipment.fulfilled_by), ('raven', 'carrier'))
        self.assertEqual(shipment.external_reference, RAVEN_ID)
        self.assertEqual(shipment.tracking_number, 'RVN7K2M9P4XA')
        self.assertEqual(shipment.tracking_url, 'https://raven.test/track/RVN7K2M9P4XA')

        _, url, create = calls[1]
        self.assertTrue(url.endswith('/v1/shipments'))
        self.assertEqual(create['headers']['X-Api-Key'], 'rk_test_key')
        self.assertEqual(create['headers']['Idempotency-Key'], shipment.shipment_id)
        body = create['json']
        self.assertEqual((body['quote_id'], body['reference']), ('q-1', shipment.shipment_id))
        self.assertEqual(body['destination']['country'], 'GH')
        self.assertEqual(body['destination']['locality'], 'Kumasi')
        # Placeholder Accra coordinates are not sent as if they were real.
        self.assertNotIn('latitude', body['destination'])
        self.assertEqual(body['parcels'][0]['weight_grams'], 2000)  # 2 × 1.0 kg
        self.assertNotIn('cod_amount_minor', body)

    def test_cash_on_delivery_tells_the_rider_what_to_collect(self):
        Order.objects.filter(pk=self.order.pk).update(payment_method='cash_on_delivery')
        sent = {}

        def fake(method, url, **kwargs):
            if url.endswith('/v1/rates'):
                return response(200, {'offers': [{'quote_id': 'q-1'}], 'refusals': []})
            sent.update(kwargs['json'])
            return response(201, raven_shipment(kwargs['json']['reference']))

        fee = mock.Mock(total=15)
        with mock.patch('order.delivery.raven.requests.request', side_effect=fake), \
                mock.patch.object(Order, 'calculate_vendor_delivery_fee', return_value=fee):
            self.assertEqual(self.book_via_seller().status_code, 201)
        self.assertEqual(sent['cod_amount_minor'], 21500)  # 200.00 goods + 15.00 delivery

    def test_a_refused_route_saves_nothing(self):
        refused = response(200, {'offers': [], 'refusals': [{'service_code': 'STANDARD', 'reason': 'NO_LANE',
                                                             'detail': 'No lane to Kumasi.'}]})
        with mock.patch('order.delivery.raven.requests.request', return_value=refused):
            res = self.book_via_seller()
        self.assertEqual(res.status_code, 502)
        self.assertIn('No lane to Kumasi', res.data['error'])
        self.assertFalse(Shipment.objects.filter(order=self.order).exists())

    def test_a_timeout_after_raven_booked_keeps_the_booking(self):
        """Deleting our side of a parcel Raven is about to collect is the worst outcome."""
        def fake(method, url, **kwargs):
            if url.endswith('/v1/rates'):
                return response(200, {'offers': [{'quote_id': 'q-1'}], 'refusals': []})
            if method == 'POST':
                raise requests.Timeout()
            return response(200, {'shipments': [raven_shipment(kwargs['params']['reference'])]})

        with mock.patch('order.delivery.raven.requests.request', side_effect=fake):
            res = self.book_via_seller()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Shipment.objects.get(order=self.order).external_reference, RAVEN_ID)

    def test_unconfigured_raven_refuses_cleanly(self):
        with override_settings(RAVEN_API_KEY=''):
            res = self.book_via_seller()
        self.assertEqual(res.status_code, 502)
        self.assertFalse(Shipment.objects.filter(order=self.order).exists())


@override_settings(**RAVEN_SETTINGS)
class CourierCancellationTests(DeliveryTestCase):

    def setUp(self):
        super().setUp()
        self.shipment = Shipment.objects.create(order=self.order, vendor=self.vendor, provider='raven',
                                                fulfilled_by='carrier', external_reference=RAVEN_ID,
                                                status='label_created')
        self.shipment.items.set([self.line])

    def cancel_order(self):
        return vendor_client(self.owner).put(f'/api/v1/vendor/orders/{self.order.pk}/status/',
                                             {'status': 'canceled'}, format='json')

    def test_cancelling_the_order_cancels_the_courier_booking(self):
        with mock.patch('order.delivery.raven.requests.request', return_value=response(200, {})) as call:
            res = self.cancel_order()
        self.assertEqual(res.status_code, 200, res.data)
        method, url = call.call_args.args
        self.assertEqual((method, url), ('POST', f'https://raven.test/api/v1/shipments/{RAVEN_ID}/cancel'))
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'canceled')

    def test_a_courier_that_refuses_leaves_the_order_open(self):
        refused = response(409, {'error': 'not_cancellable', 'detail': 'Already out for delivery.'})
        with mock.patch('order.delivery.raven.requests.request', return_value=refused):
            res = self.cancel_order()
        self.assertEqual(res.status_code, 409)
        self.assertIn('Already out for delivery', res.data['error'])
        self.order.refresh_from_db()
        self.shipment.refresh_from_db()
        self.assertNotEqual(self.order.status, 'canceled')
        self.assertEqual(self.shipment.status, 'label_created')


@override_settings(**RAVEN_SETTINGS)
class RavenWebhookTests(DeliveryTestCase):
    URL = '/api/v1/order/delivery/webhooks/raven/'

    def setUp(self):
        super().setUp()
        self.shipment = Shipment.objects.create(order=self.order, vendor=self.vendor, provider='raven',
                                                fulfilled_by='carrier', status='in_transit')
        self.shipment.items.set([self.line])

    def event(self, to='DELIVERED', event_id='d-1', reference=None):
        return {'id': event_id, 'type': 'shipments.status_changed', 'created_at': '2026-10-12T10:00:00Z',
                'data': {'shipment_id': RAVEN_ID, 'reference': reference or self.shipment.shipment_id,
                         'tracking_number': 'RVN7K2M9P4XA', 'from': 'OUT_FOR_DELIVERY', 'to': to,
                         'occurred_at': '2026-10-12T09:58:00+00:00'}}

    def post(self, body, **kwargs):
        raw, headers = signed(body, **kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            return APIClient().post(self.URL, raw, content_type='application/json', **headers)

    def test_delivery_is_applied_once_however_often_raven_retries(self):
        first = self.post(self.event())
        retry = self.post(self.event())
        self.assertEqual((first.data['applied'], retry.data['applied']), (1, 0))
        self.assertEqual(TrackingEvent.objects.filter(shipment=self.shipment).count(), 1)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'delivered')
        self.assertTrue(DeliveryWebhookEvent.objects.filter(provider='raven', event_id='d-1').exists())

    def test_matches_by_raven_id_when_no_reference_is_sent(self):
        Shipment.objects.filter(pk=self.shipment.pk).update(external_reference=RAVEN_ID)
        body = self.event(to='OUT_FOR_DELIVERY')
        del body['data']['reference']
        self.assertEqual(self.post(body).data['applied'], 1)
        self.assertEqual(TrackingEvent.objects.get(shipment=self.shipment).status, 'out_for_delivery')

    def test_an_unknown_raven_status_is_kept_not_dropped(self):
        self.post(self.event(to='SOMETHING_NEW'))
        self.assertEqual(TrackingEvent.objects.get(shipment=self.shipment).status, 'info')

    def test_a_forged_or_stale_call_is_rejected(self):
        self.assertEqual(self.post(self.event(), secret='wrong').status_code, 404)
        self.assertEqual(self.post(self.event(), timestamp=int(time.time()) - 3600).status_code, 404)
        self.assertFalse(TrackingEvent.objects.exists())

    def test_other_event_types_are_acknowledged_and_ignored(self):
        res = self.post({'id': 'x', 'type': 'billing.hold_captured', 'data': {}})
        self.assertEqual((res.status_code, res.data['applied']), (200, 0))

    def test_provider_is_registered(self):
        self.assertEqual(get_provider('raven').fulfilled_by, 'carrier')

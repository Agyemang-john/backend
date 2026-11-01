"""
Pickup points: choose a station, parcel + code, ready on delivery, station hand-over, expiry.

Run with:  DB_HOST=localhost python manage.py test pickup.tests --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from address.models import Address
from ecommerce.celery import app as celery_app
from notification.models import Notification
from order.models import Order
from pickup.models import PickupParcel, PickupPoint, PickupPointStaff
from pickup.services import expire_uncollected
from userauths.models import User

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


@override_settings(**TEST_SETTINGS)
class PickupTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        p = mock.patch('userauths.arkesel_client.ArkeselSMS')
        self.sms = p.start()
        self.addCleanup(p.stop)
        self.accra = PickupPoint.objects.create(name='Osu Hub', code='osu', address='Oxford St', town='Accra',
                                                region='Greater Accra', latitude=5.556, longitude=-0.182)
        self.kumasi = PickupPoint.objects.create(name='Adum Hub', code='adum', address='Prempeh II St', town='Kumasi',
                                                 region='Ashanti', latitude=6.692, longitude=-1.622)
        self.buyer = User.objects.create_user('Esi', 'Owusu', 'pick1@example.com', '+233254000001', 'Str0ng!Passw0rd')
        User.objects.filter(pk=self.buyer.pk).update(is_active=True)
        Address.objects.create(user=self.buyer, full_name='Esi Owusu', mobile='+233241112223', country='Ghana',
                               town='Accra', address='Home', status=True, latitude=5.6, longitude=-0.19)
        self.api = APIClient()
        self.api.force_authenticate(self.buyer)
        self.station_user = User.objects.create_user('Kwame', 'Station', 'pick2@example.com', '+233254000002', 'Str0ng!Passw0rd')
        PickupPointStaff.objects.create(point=self.accra, user=self.station_user)

    def test_nearest_first_and_search(self):
        res = APIClient().get('/api/v1/pickup/points/', {'lat': 6.7, 'lon': -1.6})
        self.assertEqual([r['code'] for r in res.data['results']], ['adum', 'osu'])
        res = APIClient().get('/api/v1/pickup/points/', {'q': 'accra'})
        self.assertEqual([r['code'] for r in res.data['results']], ['osu'])

    def place_pickup_order(self):
        res = self.api.post('/api/v1/pickup/select/', {'point': self.accra.pk}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        addr = Address.objects.get(user=self.buyer, status=True)
        self.assertEqual(addr.pickup_point, self.accra)
        self.assertEqual((addr.latitude, addr.full_name), (5.556, 'Esi Owusu'))
        with self.captureOnCommitCallbacks(execute=True):
            order = Order.objects.create(user=self.buyer, address=addr, total=Decimal('50'), is_ordered=True,
                                         order_number='ORD-PICK-1')
        return order

    def test_full_flow(self):
        order = self.place_pickup_order()
        parcel = PickupParcel.objects.get(order=order)
        self.assertEqual((parcel.status, len(parcel.code)), (PickupParcel.AWAITING, 6))

        with self.captureOnCommitCallbacks(execute=True):
            order.status = 'delivered'
            order.save()
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, PickupParcel.READY)
        self.assertTrue(Notification.objects.filter(recipient=self.buyer, data__message__icontains=parcel.code).exists())
        self.assertEqual(self.api.get(f'/api/v1/pickup/orders/{order.pk}/').data['code'], parcel.code)

        station = APIClient()
        station.force_authenticate(self.station_user)
        self.assertEqual(len(station.get('/api/v1/pickup/station/parcels/').data['results']), 1)
        wrong = station.post('/api/v1/pickup/station/collect/', {'code': parcel.code, 'order_number': 'ORD-X'}, format='json')
        self.assertEqual(wrong.status_code, 409)
        res = station.post('/api/v1/pickup/station/collect/', {'code': parcel.code, 'order_number': 'ORD-PICK-1'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, PickupParcel.COLLECTED)
        self.assertIsNone(self.api.get(f'/api/v1/pickup/orders/{order.pk}/').data['code'], "code hidden once used")

    def test_only_station_staff_hand_over(self):
        order = self.place_pickup_order()
        parcel = PickupParcel.objects.get(order=order)
        res = self.api.post('/api/v1/pickup/station/collect/', {'code': parcel.code}, format='json')
        self.assertEqual(res.status_code, 403)

    def test_expiry(self):
        order = self.place_pickup_order()
        parcel = PickupParcel.objects.get(order=order)
        PickupParcel.objects.filter(pk=parcel.pk).update(status=PickupParcel.READY,
                                                         expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(expire_uncollected(), 1)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, PickupParcel.EXPIRED)

    def test_home_delivery_has_no_parcel(self):
        addr = Address.objects.get(user=self.buyer, status=True)
        with self.captureOnCommitCallbacks(execute=True):
            Order.objects.create(user=self.buyer, address=addr, total=Decimal('50'), is_ordered=True, order_number='ORD-HOME')
        self.assertFalse(PickupParcel.objects.exists())

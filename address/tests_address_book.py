"""
Address book v2: country formats + normalising, first/duplicate/limit rules,
orders keeping the address they shipped to (copy-on-write edits, archive on
delete), default promotion, and the server-side address search.

Run with:  DB_HOST=localhost python manage.py test address.tests_address_book --keepdb
"""

from decimal import Decimal
from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from address import formats, geo, services
from address.models import Address
from ecommerce.celery import app as celery_app
from order.models import Order
from userauths.models import Profile, User

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
    'GOOGLE_MAPS_API_KEY': '',
    'ADDRESS_GEO_PROVIDER': 'photon',
}

HOME = {
    'full_name': 'Ama Mensah', 'mobile': '024 123 4567', 'country_code': 'GH',
    'region': 'greater accra region', 'town': 'Accra', 'address': '12 Ring Road East',
    'latitude': 5.57, 'longitude': -0.19, 'location_source': 'search',
}


def make_user(n):
    user = User.objects.create_user(f'Book{n}', 'Test', f'book{n}@example.com', f'+23324300{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class FormatTests(TestCase):
    def test_country_resolution(self):
        self.assertEqual(formats.resolve_country('GH'), ('Ghana', 'GH'))
        self.assertEqual(formats.resolve_country('ghana'), ('Ghana', 'GH'))
        self.assertEqual(formats.resolve_country('Ivory Coast')[1], 'CI')
        self.assertEqual(formats.resolve_country('UK')[1], 'GB')
        self.assertEqual(formats.resolve_country('Narnia'), (None, None))
        # Longer than the old 20-character column.
        self.assertEqual(formats.resolve_country('BA'), ('Bosnia and Herzegovina', 'BA'))

    def test_phone(self):
        self.assertEqual(formats.normalize_phone('024 123 4567', 'GH'), '+233241234567')
        self.assertEqual(formats.normalize_phone('233241234567', 'GH'), '+233241234567')
        self.assertEqual(formats.normalize_phone('+44 7700 900123', 'GH'), '+447700900123')
        self.assertEqual(formats.normalize_phone('0044 7700 900123', 'GH'), '+447700900123')
        with self.assertRaises(formats.InvalidValue):
            formats.normalize_phone('12', 'GH')

    def test_ghana_digital_address(self):
        self.assertEqual(formats.normalize_postal('ga1838164', 'GH'), 'GA-183-8164')
        self.assertEqual(formats.normalize_postal('AK 485 9323', 'GH'), 'AK-485-9323')
        with self.assertRaises(formats.InvalidValue):
            formats.normalize_postal('12345', 'GH')
        self.assertEqual(formats.region_from_digital_address('GA-183-8164'), 'Greater Accra')

    def test_format_endpoint(self):
        res = APIClient().get('/api/v1/address/formats/gh/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['dial_code'], '233')
        self.assertEqual(len(res.data['regions']), 16)
        self.assertIn('Greater Accra', res.data['regions'])
        self.assertEqual(res.data['postal_example'], 'GA-183-8164')
        us = APIClient().get('/api/v1/address/formats/US/').data
        self.assertTrue(us['postal_required'])
        self.assertEqual(APIClient().get('/api/v1/address/formats/zz/').status_code, 404)


@override_settings(**TEST_SETTINGS)
class AddressBookTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user(1)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def add(self, **over):
        res = self.api.post('/api/v1/address/addresses/', {**HOME, **over}, format='json')
        return res

    def test_first_address_is_default_and_normalised(self):
        res = self.add()
        self.assertEqual(res.status_code, 201, res.content)
        a = Address.objects.get(pk=res.data['id'])
        self.assertTrue(a.status)
        self.assertEqual((a.country, a.country_code, a.region), ('Ghana', 'GH', 'Greater Accra'))
        self.assertEqual(a.mobile, '+233241234567')
        self.assertEqual(a.location_source, 'search')
        self.assertEqual(Profile.objects.get(user=self.user).mobile, '+233241234567')
        self.assertEqual(res.cookies['ship_country'].value, 'GH')
        self.assertTrue(res.data['is_precise'])

    def test_older_clients_sending_a_country_name(self):
        res = self.add(country_code='', country='Ghana')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['country_code'], 'GH')

    def test_required_fields_follow_the_country(self):
        res = self.add(mobile='', region='', town='')
        self.assertEqual(res.status_code, 400)
        self.assertIn('mobile', res.data)
        self.assertIn('town', res.data)
        res = self.add(country_code='US', region='New York', gps_address='', mobile='+1 212 555 0100')
        self.assertIn('gps_address', res.data)  # ZIP code required in the US
        res = self.add(gps_address='12345')
        self.assertIn('gps_address', res.data)

    def test_digital_address_fills_the_region(self):
        res = self.add(region='', gps_address='ak4859323')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.data['region'], res.data['gps_address']), ('Ashanti', 'AK-485-9323'))

    def test_same_place_twice_updates_the_first(self):
        first = self.add().data
        res = self.add(address='12  ring road EAST', mobile='0201112222', landmark='Opposite the mall')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['merged'])
        self.assertEqual(res.data['id'], first['id'])
        self.assertEqual(Address.objects.filter(user=self.user).count(), 1)
        self.assertEqual(res.data['landmark'], 'Opposite the mall')

    def test_second_address_not_default_unless_asked(self):
        self.add()
        second = self.add(address='5 Oxford Street', label='work').data
        self.assertFalse(second['status'])
        third = self.add(address='9 Spintex Road', status=True).data
        self.assertTrue(third['status'])
        self.assertEqual(Address.objects.filter(user=self.user, status=True).count(), 1)

    def test_limit(self):
        with mock.patch.object(services, 'MAX_ADDRESSES', 2):
            self.add()
            self.add(address='2 Second St')
            res = self.add(address='3 Third St')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'address_limit')

    def test_edit_unused_address_in_place(self):
        a = self.add().data
        res = self.api.patch(f"/api/v1/address/addresses/{a['id']}/", {'address_line2': 'Flat 3'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['id'], a['id'])
        self.assertIsNone(res.data['replaced_id'])
        self.assertEqual(res.data['street_lines'], ['12 Ring Road East', 'Flat 3'])

    def test_orders_keep_the_address_they_shipped_to(self):
        a = self.add().data
        order = Order.objects.create(user=self.user, address_id=a['id'], total=Decimal('10'), is_ordered=True,
                                     order_number='ORD-BOOK-1')
        self.assertIsNotNone(Address.objects.get(pk=a['id']).last_used_at)

        res = self.api.put(f"/api/v1/address/addresses/{a['id']}/",
                           {**HOME, 'address': '40 Independence Ave', 'latitude': 5.58, 'longitude': -0.2}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        new_id = res.data['id']
        self.assertNotEqual(new_id, a['id'])
        self.assertEqual(res.data['replaced_id'], a['id'])
        self.assertTrue(res.data['status'])  # the default moved with it

        order.refresh_from_db()
        self.assertEqual(order.address.address, '12 Ring Road East')
        old = Address.objects.get(pk=a['id'])
        self.assertIsNotNone(old.archived_at)
        self.assertEqual(old.replaced_by_id, new_id)
        listed = self.api.get('/api/v1/address/addresses/').data
        self.assertEqual([x['id'] for x in listed], [new_id])
        self.assertEqual(self.api.get(f"/api/v1/address/addresses/{a['id']}/").status_code, 404)

    def test_resaving_a_used_address_unchanged_keeps_it(self):
        a = self.add(email='').data
        Address.objects.filter(pk=a['id']).update(email=None, gps_address=None)
        Order.objects.create(user=self.user, address_id=a['id'], total=Decimal('10'), is_ordered=True,
                             order_number='ORD-BOOK-3')
        res = self.api.put(f"/api/v1/address/addresses/{a['id']}/", {**HOME, 'email': '', 'gps_address': ''}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['id'], a['id'])
        self.assertEqual(Address.objects.filter(user=self.user).count(), 1)

    def test_delete_archives_used_and_promotes_a_new_default(self):
        a = self.add().data
        b = self.add(address='5 Oxford Street').data
        Order.objects.create(user=self.user, address_id=a['id'], total=Decimal('10'), is_ordered=True,
                             order_number='ORD-BOOK-2')
        res = self.api.delete(f"/api/v1/address/addresses/{a['id']}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['default_id'], b['id'])
        self.assertTrue(Address.objects.filter(pk=a['id'], archived_at__isnull=False).exists())
        self.assertTrue(Address.objects.get(pk=b['id']).status)
        # Unused: really deleted.
        self.api.delete(f"/api/v1/address/addresses/{b['id']}/")
        self.assertFalse(Address.objects.filter(pk=b['id']).exists())
        self.assertEqual(self.api.get('/api/v1/address/addresses/').data, [])

    def test_make_default_and_other_users(self):
        a = self.add().data
        b = self.add(address='5 Oxford Street').data
        res = self.api.put('/api/v1/address/addresses/default/', {'id': b['id']}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.api.get('/api/v1/address/addresses/default/').data['id'], b['id'])
        self.assertEqual(self.api.get('/api/v1/order/address/default/').data['id'], b['id'])
        other = APIClient()
        other.force_authenticate(make_user(2))
        self.assertEqual(other.get(f"/api/v1/address/addresses/{a['id']}/").status_code, 404)
        self.assertEqual(other.put('/api/v1/address/addresses/default/', {'id': a['id']}, format='json').status_code, 404)

    def test_typed_address_is_looked_up(self):
        with mock.patch('address.services.geo.geocode', return_value=(6.69, -1.62, 'street')) as g:
            res = self.add(latitude=None, longitude=None, location_source='', town='Kumasi', region='Ashanti')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.data['latitude'], res.data['location_source']), (6.69, 'geocoded'))
        self.assertFalse(res.data['is_precise'])
        self.assertIn('Kumasi', g.call_args[0][0])

    def test_nothing_found_is_marked_approximate(self):
        with mock.patch('address.services.geo.geocode', return_value=None):
            res = self.add(latitude=None, longitude=None, location_source='')
        self.assertEqual(res.data['location_source'], 'approx')
        self.assertIsNotNone(res.data['latitude'])  # fees still have a point

    def test_contact_edit_keeps_the_pin(self):
        a = self.add().data
        with mock.patch('address.services.geo.geocode') as g:
            res = self.api.patch(f"/api/v1/address/addresses/{a['id']}/", {'full_name': 'Ama K. Mensah'}, format='json')
        g.assert_not_called()
        self.assertEqual((res.data['latitude'], res.data['location_source']), (5.57, 'search'))


def _photon(features):
    resp = mock.Mock()
    resp.json.return_value = {'features': features}
    resp.raise_for_status.return_value = None
    return resp


def _feature(name, street=None, number=None, city='Accra', country='Ghana', cc='GH', lon=-0.18, lat=5.6):
    return {
        'geometry': {'coordinates': [lon, lat]},
        'properties': {'name': name, 'street': street, 'housenumber': number, 'city': city,
                       'state': 'Greater Accra', 'country': country, 'countrycode': cc, 'osm_key': 'amenity'},
    }


@override_settings(**TEST_SETTINGS)
class GeoTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.api.force_authenticate(make_user(3))

    def test_search_filters_country_caches_and_resolves(self):
        features = [_feature('Accra Mall', street='Spintex Road'),
                    _feature('Accra Bar', city='Lagos', country='Nigeria', cc='NG'),
                    _feature(None, street='Ring Road', number='12')]
        with mock.patch('address.geo.requests.get', return_value=_photon(features)) as get:
            res = self.api.get('/api/v1/address/search/', {'q': 'accra', 'country': 'gh', 'lat': 5.61, 'lon': -0.2})
            again = self.api.get('/api/v1/address/search/', {'q': 'Accra ', 'country': 'GH', 'lat': 5.64, 'lon': -0.2})
        self.assertEqual(get.call_count, 1)  # second call served from cache
        self.assertEqual(res.status_code, 200)
        titles = [r['title'] for r in res.data['results']]
        self.assertEqual(titles, ['Accra Mall, Spintex Road', '12 Ring Road'])
        self.assertEqual(again.data['results'], res.data['results'])
        self.assertEqual(res.data['results'][1]['precision'], 'exact')

        place = self.api.get('/api/v1/address/place/', {'ref': res.data['results'][0]['ref']})
        self.assertEqual(place.status_code, 200)
        self.assertEqual((place.data['town'], place.data['country_code'], place.data['latitude']), ('Accra', 'GH', 5.6))
        self.assertEqual(self.api.get('/api/v1/address/place/', {'ref': 'p:nope'}).status_code, 404)

    def test_short_queries_skip_the_provider(self):
        with mock.patch('address.geo.requests.get') as get:
            res = self.api.get('/api/v1/address/search/', {'q': 'ac'})
        get.assert_not_called()
        self.assertEqual(res.data['results'], [])

    def test_provider_down(self):
        with mock.patch('address.geo.requests.get', side_effect=requests.ConnectionError()):
            res = self.api.get('/api/v1/address/search/', {'q': 'accra mall'})
            self.assertIsNone(geo.geocode('accra mall'))
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data['code'], 'geo_unavailable')

    def test_search_needs_sign_in_but_reverse_does_not(self):
        self.assertEqual(APIClient().get('/api/v1/address/search/', {'q': 'accra'}).status_code, 401)
        with mock.patch('address.geo.requests.get', return_value=_photon([_feature('Osu Castle')])):
            res = APIClient().get('/api/v1/address/reverse/', {'lat': 5.5501, 'lon': -0.1801})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data['country_code'], res.data['latitude']), ('GH', 5.5501))
        self.assertEqual(APIClient().get('/api/v1/address/reverse/', {'lat': 'x'}).status_code, 400)

    @override_settings(GOOGLE_MAPS_API_KEY='k', ADDRESS_GEO_PROVIDER='')
    def test_google_provider(self):
        auto = mock.Mock(status_code=200)
        auto.json.return_value = {'suggestions': [{'placePrediction': {
            'placeId': 'abc', 'structuredFormat': {'mainText': {'text': 'Accra Mall'},
                                                   'secondaryText': {'text': 'Spintex Rd, Accra'}}}}]}
        details = mock.Mock(status_code=200)
        details.json.return_value = {
            'displayName': {'text': 'Accra Mall'}, 'formattedAddress': 'Spintex Rd, Accra, Ghana',
            'location': {'latitude': 5.62, 'longitude': -0.17},
            'addressComponents': [
                {'longText': 'Spintex Road', 'shortText': 'Spintex Rd', 'types': ['route']},
                {'longText': 'Accra', 'shortText': 'Accra', 'types': ['locality']},
                {'longText': 'Greater Accra Region', 'shortText': 'GA', 'types': ['administrative_area_level_1']},
                {'longText': 'Ghana', 'shortText': 'GH', 'types': ['country']},
            ]}
        with mock.patch('address.geo.requests.post', return_value=auto), \
                mock.patch('address.geo.requests.get', return_value=details):
            res = self.api.get('/api/v1/address/search/', {'q': 'accra mall', 'country': 'GH', 'session': 's1'})
            self.assertEqual(res.data['provider'], 'google')
            ref = res.data['results'][0]['ref']
            self.assertEqual(ref, 'g:abc')
            place = self.api.get('/api/v1/address/place/', {'ref': ref, 'session': 's1'}).data
        self.assertEqual(place['line1'], 'Accra Mall, Spintex Road')
        self.assertEqual((place['town'], place['country_code'], place['latitude']), ('Accra', 'GH', 5.62))

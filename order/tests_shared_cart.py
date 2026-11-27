"""
order/tests_shared_cart.py
Sharing a cart with friends and family: guests and signed-in shoppers can
share; anyone with the link can add the items to their own cart.

Run with:  DB_HOST=localhost python manage.py test order.tests_shared_cart --keepdb
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from order.models import CartItem, SharedCart
from product.models import Product, Variants
from vendor.tests_operations import OpsTestCase, make_user

ADD = '/api/v1/order/add-to-cart/'
SHARE = '/api/v1/order/cart/share/'


def shared(token):
    return f'/api/v1/order/cart/shared/{token}/'


class SharedCartTests(OpsTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        Product.objects.filter(pk=self.product.pk).update(total_quantity=5, status='published')
        self.product.refresh_from_db()
        self.shake = Product.objects.create(
            title='Shake', sub_category=self.sub, vendor=self.vendor, status='published', variant='Options',
            variation_axes=[{'key': 'flavor', 'label': 'Flavour'}], price=Decimal('40'), total_quantity=0)
        self.vanilla = Variants.objects.create(product=self.shake, options={'flavor': 'Vanilla'}, price=Decimal('40'), quantity=2)

        self.sharer = APIClient()
        self.sharer.force_authenticate(self.customer)
        self.sharer.post(ADD, {'product_id': self.product.id, 'quantity': 2}, format='json')
        self.sharer.post(ADD, {'product_id': self.shake.id, 'variant_id': self.vanilla.id, 'quantity': 2}, format='json')

    def share(self, client=None, **data):
        return (client or self.sharer).post(SHARE, data, format='json')

    def test_signed_in_shopper_shares_and_a_guest_adds_everything(self):
        res = self.share(name='Ama', note='For the party <b>Saturday</b>')
        self.assertEqual(res.status_code, 201, res.data)
        token = res.data['token']
        self.assertEqual(res.data['path'], f'/cart/shared/{token}')

        guest = APIClient()
        page = guest.get(shared(token), HTTP_X_CURRENCY='USD')
        self.assertEqual(page.status_code, 200, page.data)
        self.assertEqual((page.data['sender_name'], page.data['currency']), ('Ama', 'USD'))
        self.assertEqual(page.data['note'], 'For the party <b>Saturday</b>', 'kept as text; the page never renders HTML')
        lines = {i['title']: i for i in page.data['items']}
        self.assertEqual(lines['Shake']['option_label'], 'Vanilla')
        self.assertEqual(lines['Shake']['unit_price'], 4.0, 'today\'s price in the shopper\'s currency')
        self.assertFalse(page.data['is_owner'])

        res = guest.post(f'{shared(token)}add/', {}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['total_cart_quantity'], 4)
        cart = guest.get('/api/v1/order/cart/').data
        self.assertEqual(sorted((i['product']['title'], i['quantity']) for i in cart['items']),
                         sorted([('Shake', 2), (self.product.title, 2)]))
        share = SharedCart.objects.get(token=token)
        self.assertEqual(share.adds, 1)

    def test_guest_shares_and_the_same_cart_gives_the_same_link(self):
        guest = APIClient()
        guest.post(ADD, {'product_id': self.product.id, 'quantity': 1}, format='json')
        first = self.share(guest)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(self.share(guest).data['token'], first.data['token'])
        self.assertTrue(guest.get(shared(first.data['token'])).data['is_owner'])

        guest.post(ADD, {'product_id': self.product.id, 'quantity': 1}, format='json')
        self.assertNotEqual(self.share(guest).data['token'], first.data['token'], 'a changed cart is a new link')

    def test_recipient_picks_items_and_stock_is_respected(self):
        token = self.share().data['token']
        friend = make_user(9)
        client = APIClient()
        client.force_authenticate(friend)
        # The friend already holds 1 vanilla; only 1 more fits (stock 2).
        client.post(ADD, {'product_id': self.shake.id, 'variant_id': self.vanilla.id, 'quantity': 1}, format='json')
        res = client.post(f'{shared(token)}add/', {'items': [
            {'product_id': self.shake.id, 'variant_id': self.vanilla.id, 'quantity': 2},
            {'product_id': 999999, 'quantity': 1},                     # never shared: ignored
        ]}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['added'], [{'product_id': self.shake.id, 'variant_id': self.vanilla.id,
                                              'title': 'Shake', 'quantity': 1, 'limited': True}])
        self.assertEqual(CartItem.objects.get(cart__user=friend, variant=self.vanilla).quantity, 2)
        self.assertFalse(CartItem.objects.filter(cart__user=friend, product=self.product).exists())

    def test_items_taken_off_sale_show_as_unavailable(self):
        token = self.share().data['token']
        Product.objects.filter(pk=self.shake.pk).update(status='draft')
        page = APIClient().get(shared(token)).data
        self.assertEqual({i['title']: i['status'] for i in page['items']}, {'Shake': 'unavailable', self.product.title: 'ok'})
        self.assertEqual(page['total'], float(self.product.price * 2))
        res = APIClient().post(f'{shared(token)}add/', {}, format='json')
        self.assertEqual([s['reason'] for s in res.data['skipped']], ['unavailable'])

    def test_empty_cart_expired_and_unknown_links(self):
        res = self.share(APIClient())
        self.assertEqual(res.status_code, 400)
        self.assertIn('empty', res.data['detail'])

        token = self.share().data['token']
        SharedCart.objects.filter(token=token).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(APIClient().get(shared(token)).status_code, 404)
        self.assertEqual(APIClient().get(shared('nope')).status_code, 404)
        self.assertEqual(APIClient().post(f"{shared('nope')}add/", {}, format='json').status_code, 404)

    def test_views_count_only_real_visits_by_others(self):
        token = self.share().data['token']
        self.sharer.get(shared(token), {'view': '1'})          # the sharer checking their own link
        APIClient().get(shared(token))                          # a link preview / server render
        APIClient().get(shared(token), {'view': '1'})
        self.assertEqual(SharedCart.objects.get(token=token).views, 1)

"""
order/tests_cart_stock.py
The cart never holds more than is in stock (the product page's "Max N").

Run with:  DB_HOST=localhost python manage.py test order.tests_cart_stock --keepdb
"""

from decimal import Decimal

from rest_framework.test import APIClient

from order.models import CartItem
from product.models import Product, Variants
from vendor.tests_operations import OpsTestCase

URL = '/api/v1/order/add-to-cart/'


class CartStockCapTests(OpsTestCase):

    def setUp(self):
        super().setUp()
        Product.objects.filter(pk=self.product.pk).update(total_quantity=3)
        self.product.refresh_from_db()
        self.client = APIClient()
        self.client.force_authenticate(self.customer)

    def add(self, change=1, variant=None, client=None):
        return (client or self.client).post(URL, {
            'product_id': self.product.id,
            'variant_id': variant.id if variant else None,
            'quantity': change,
        }, format='json')

    def test_adds_up_to_stock_then_refuses(self):
        for expected in (1, 2, 3):
            res = self.add()
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.data['quantity'], expected)
            self.assertEqual(res.data['available_stock'], 3)
        self.assertTrue(res.data['is_out_of_stock'], 'the last unit is in the cart')

        res = self.add()
        self.assertEqual(res.status_code, 400)
        self.assertEqual((res.data['quantity'], res.data['available_stock']), (3, 3))
        self.assertIn('maximum', res.data['detail'])
        self.assertEqual(CartItem.objects.get(cart__user=self.customer).quantity, 3)

    def test_lowering_is_allowed_even_above_stock(self):
        self.add(3)
        Product.objects.filter(pk=self.product.pk).update(total_quantity=1)  # stock sold elsewhere
        self.assertEqual(self.add().status_code, 400)
        res = self.add(-1)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['quantity'], 2)

    def test_out_of_stock_refused(self):
        Product.objects.filter(pk=self.product.pk).update(total_quantity=0)
        res = self.add()
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['available_stock'], 0)
        self.assertFalse(CartItem.objects.filter(cart__user=self.customer).exists())

    def test_variant_uses_its_own_stock(self):
        Product.objects.filter(pk=self.product.pk).update(variant='Size', total_quantity=99)
        self.product.refresh_from_db()
        variant = Variants.objects.create(product=self.product, quantity=2, price=Decimal('100.00'))
        self.assertEqual(self.add(2, variant).status_code, 200)
        res = self.add(1, variant)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['available_stock'], 2)

    def test_guest_cart_capped(self):
        guest = APIClient()
        self.assertEqual(self.add(2, client=guest).status_code, 200)
        res = self.add(2, client=guest)
        self.assertEqual(res.status_code, 400)
        self.assertIn('1 more', res.data['detail'])

    def test_cart_lines_report_stock(self):
        self.add(2)
        res = self.client.get('/api/v1/order/cart/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['items'][0]['available_stock'], 3)

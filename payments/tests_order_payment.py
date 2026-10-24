"""
payments/tests_order_payment.py
Checkout payment verification must not be fooled by the browser: the amount
in the Paystack popup is set client-side, and any Paystack reference can be
sent to the verify endpoint.

Run with:  DB_HOST=localhost python manage.py test payments.tests_order_payment --keepdb
"""

from decimal import Decimal
from unittest import mock

from rest_framework.test import APIClient

from address.models import Address
from order.models import Cart, CartItem
from payments.models import Payment, PaymentTransaction
from product.tests_flash_sales import FlashSaleTestCase, make_user

VERIFY = '/api/v1/payments/verify-payment/{}/'


def paystack(reference, amount, currency='GHS', status='success'):
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.json.return_value = {'status': True, 'data': {
        'status': status, 'reference': reference, 'amount': amount, 'currency': currency,
        'customer': {'email': 'buyer@example.com'},
    }}
    return response


class OrderPaymentVerifyTests(FlashSaleTestCase):

    def setUp(self):
        super().setUp()
        self.task = mock.patch('payments.views.create_order_from_payment_task.delay').start()
        self.get = mock.patch('payments.views.requests.get').start()
        self.addCleanup(mock.patch.stopall)
        self.customer_api = self.shopper(self.customer)
        # Cart: 2 x 100.00 GHS, no delivery option, so the grand total is 200.00.
        self.cart = self.fill_cart(self.customer)

    def shopper(self, user):
        Address.objects.get_or_create(user=user, status=True, defaults=dict(
            full_name='Ama', region='Greater Accra', town='Accra', address='1 Test St'))
        api = APIClient()
        api.force_authenticate(user)
        return api

    def fill_cart(self, user):
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=2)
        return cart

    def expected_pesewas(self):
        return int(Cart.objects.get(pk=self.cart.pk).calculate_grand_total() * 100)

    def test_full_payment_creates_order(self):
        self.get.return_value = paystack('REF-OK', self.expected_pesewas())
        res = self.customer_api.get(VERIFY.format('REF-OK'))
        self.assertEqual(res.status_code, 200, res.data)
        self.task.assert_called_once()
        self.assertTrue(Payment.objects.filter(ref='REF-OK', user=self.customer).exists())

    def test_underpayment_is_refused(self):
        self.get.return_value = paystack('REF-LOW', 100)   # 1 cedi for a 200 cedi cart
        res = self.customer_api.get(VERIFY.format('REF-LOW'))
        self.assertEqual(res.status_code, 400)
        self.task.assert_not_called()
        self.assertFalse(Payment.objects.filter(ref='REF-LOW').exists())

    def test_other_currency_is_refused(self):
        self.get.return_value = paystack('REF-NGN', self.expected_pesewas(), currency='NGN')
        self.assertEqual(self.customer_api.get(VERIFY.format('REF-NGN')).status_code, 400)
        self.task.assert_not_called()

    def test_reference_cannot_be_reused_by_another_account(self):
        self.get.return_value = paystack('REF-ONE', self.expected_pesewas())
        self.assertEqual(self.customer_api.get(VERIFY.format('REF-ONE')).status_code, 200)

        thief = make_user(3)
        self.fill_cart(thief)
        res = self.shopper(thief).get(VERIFY.format('REF-ONE'))
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.task.call_count, 1)

    def test_same_account_repeat_does_not_create_second_order(self):
        self.get.return_value = paystack('REF-TWICE', self.expected_pesewas())
        self.customer_api.get(VERIFY.format('REF-TWICE'))
        self.assertEqual(self.customer_api.get(VERIFY.format('REF-TWICE')).status_code, 200)
        self.assertEqual(self.task.call_count, 1)

    def test_subscription_payment_cannot_buy_goods(self):
        PaymentTransaction.objects.create(vendor=self.vendor, transaction_type='initial',
                                          amount=Decimal('500.00'), paystack_reference='SUB-PAID')
        self.get.return_value = paystack('SUB-PAID', 50000)
        self.assertEqual(self.customer_api.get(VERIFY.format('SUB-PAID')).status_code, 409)
        self.task.assert_not_called()

    def test_mismatched_reference_is_refused(self):
        self.get.return_value = paystack('SOMEONE-ELSES', self.expected_pesewas())
        self.assertEqual(self.customer_api.get(VERIFY.format('REF-MINE')).status_code, 400)
        self.task.assert_not_called()

    def test_secret_key_is_never_printed(self):
        self.get.return_value = paystack('REF-PRINT', self.expected_pesewas())
        with mock.patch('builtins.print') as printed:
            self.customer_api.get(VERIFY.format('REF-PRINT'))
        printed.assert_not_called()

"""
Order risk scoring, holds, staff decisions and the pay-on-delivery gate.

Run with:  DB_HOST=localhost python manage.py test risk.tests --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from order.models import Order, OrderProduct
from product.models import Brand, Category, Main_Category, Product, Sub_Category
from risk import services
from risk.models import OrderRisk
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


def make_user(n, verified=True, joined_days_ago=60):
    user = User.objects.create_user(f'Rsk{n}', 'Test', f'rsk{n}@example.com', f'+23325000{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now() if verified else None,
                                           date_joined=timezone.now() - timedelta(days=joined_days_ago))
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class RiskTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(name='Rsk Shop', user=self.owner, email='rsk-shop@example.com',
                                            contact='+233550002222', is_approved=True, status='VERIFIED')
        main = Main_Category.objects.create(title='Rsk Main')
        sub = Sub_Category.objects.create(title='Rsk Sub', category=Category.objects.create(title='Rsk Cat', main_category=main))
        self.product = Product.objects.create(title='Rsk Laptop', sub_category=sub, vendor=self.vendor, status='published',
                                              price=Decimal('5000'), total_quantity=20, brand=Brand.objects.create(title='Rsk Brand'))

    def order(self, user, total='5000', method='paystack', status='pending', qty=1):
        with self.captureOnCommitCallbacks(execute=True):
            o = Order.objects.create(user=user, total=Decimal(total), is_ordered=True, payment_method=method,
                                     status=status, order_number=f'ORD-RSK-{Order.objects.count()}', ip='10.0.0.1')
            OrderProduct.objects.create(order=o, product=self.product, quantity=qty, price=Decimal(total) / qty,
                                        amount=Decimal(total))
        return o

    def test_trusted_customer_is_low(self):
        o = self.order(make_user(2))
        risk = services.assess(o)
        self.assertEqual((risk.level, risk.status), (OrderRisk.LOW, OrderRisk.CLEAR))

    def test_new_unverified_big_order_is_held_and_blocks_shipping(self):
        fresh = make_user(3, verified=False, joined_days_ago=0)
        for _ in range(2):
            self.order(fresh, total='100')
        o = self.order(fresh, total='5000', qty=12)
        risk = OrderRisk.objects.get(order=o)
        self.assertEqual(risk.level, OrderRisk.HIGH, risk.reasons)
        self.assertEqual(risk.status, OrderRisk.HELD)
        codes = {r['code'] for r in risk.reasons}
        self.assertTrue({'new_account', 'unverified', 'velocity', 'bulk_quantity'} <= codes)

        o.vendors.add(self.vendor)
        seller = APIClient()
        seller.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        seller.credentials(HTTP_X_USER_TYPE='vendor')
        res = seller.post(f'/api/v1/vendor/orders/{o.pk}/shipment/', {}, format='json')
        self.assertEqual(res.status_code, 409)

        services.approve(risk, None)
        self.assertFalse(services.is_held(o.pk))

    def test_cancel_as_fraud_restocks(self):
        fresh = make_user(4, verified=False, joined_days_ago=0)
        o = self.order(fresh, total='5000', qty=12)
        risk = OrderRisk.objects.get(order=o)
        services.cancel_as_fraud(risk, None, 'Stolen card')
        self.product.refresh_from_db()
        self.assertEqual(self.product.total_quantity, 32)
        o.refresh_from_db()
        self.assertEqual(o.status, 'canceled')
        from order.models import Refund
        self.assertTrue(Refund.objects.filter(order_product__order=o).exists(), 'prepaid order: refund queued')

    def test_cod_refusals(self):
        user = make_user(5)
        self.assertEqual(services.cod_allowed(user)[0], True)
        self.order(user, total='100', method='cash_on_delivery', status='canceled')
        o = self.order(user, total='100', method='cash_on_delivery')
        self.assertIn('cod_refusals', {r['code'] for r in OrderRisk.objects.get(order=o).reasons})
        self.order(user, total='100', method='cash_on_delivery', status='canceled')
        self.assertEqual(services.cod_allowed(user)[0], False)

    def test_assess_is_idempotent(self):
        o = self.order(make_user(6))
        self.assertEqual(services.assess(o).pk, services.assess(o).pk)
        self.assertEqual(OrderRisk.objects.filter(order=o).count(), 1)

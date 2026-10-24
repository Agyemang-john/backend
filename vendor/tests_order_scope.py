"""
vendor/tests_order_scope.py
In an order with items from two stores, each store can only change its own
part: its lines and its shipments.

Run with:  DB_HOST=localhost python manage.py test vendor.tests_order_scope --keepdb
"""

from decimal import Decimal
from unittest import mock

from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from order.models import Order, OrderProduct, Shipment
from product.models import Product
from product.tests_flash_sales import FlashSaleTestCase, make_user
from userauths.session_utils import register_session
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor


class SharedOrderTests(FlashSaleTestCase):

    def setUp(self):
        super().setUp()
        celery_app.conf.task_always_eager = True
        self.addCleanup(setattr, celery_app.conf, 'task_always_eager', False)
        for target in ('notification.utils.send_notification',):
            mock.patch(target).start()
        self.addCleanup(mock.patch.stopall)

        other_owner = make_user(5)
        self.other = Vendor.objects.create(name='Other Shop', user=other_owner, email='other@example.com',
                                           contact='+233550005555', is_approved=True, status='VERIFIED')
        other_product = Product.objects.create(title='Other Lamp', sub_category=self.sub, vendor=self.other,
                                               status='published', price=Decimal('50.00'), brand=self.brand)
        self.order = Order.objects.create(user=self.customer, total=Decimal('150'), is_ordered=True,
                                          status='processing')
        self.order.vendors.add(self.vendor, self.other)
        self.mine = OrderProduct.objects.create(order=self.order, product=self.product, quantity=1,
                                                price=Decimal('100'), amount=Decimal('100'), status='processing')
        self.theirs = OrderProduct.objects.create(order=self.order, product=other_product, quantity=1,
                                                  price=Decimal('50'), amount=Decimal('50'), status='processing')
        self.their_shipment = Shipment.objects.create(order=self.order, vendor=self.other)

        refresh = CustomVendorRefreshToken.for_user(self.owner)
        register_session(self.owner, refresh['jti'], APIClient().get('/').wsgi_request, is_vendor=True)
        self.api = APIClient()
        self.api.cookies['vendor_access'] = str(refresh.access_token)
        self.api.credentials(HTTP_X_USER_TYPE='vendor')

    def put_status(self, value):
        return self.api.put(f'/api/v1/vendor/orders/{self.order.id}/status/', {'status': value}, format='json')

    def test_cancelling_my_part_leaves_the_other_store_alone(self):
        with mock.patch('order.fulfilment.cancel_shipment') as cancel:
            res = self.put_status('canceled')
        self.assertEqual(res.status_code, 200, res.content)
        cancel.assert_not_called()                     # their shipment is not mine to cancel
        self.mine.refresh_from_db()
        self.theirs.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.mine.status, 'canceled')
        self.assertEqual(self.theirs.status, 'processing')
        self.assertEqual(self.order.status, 'processing')   # the other store's part is still live

    def test_shipping_my_part_marks_order_shipped_without_touching_theirs(self):
        self.assertEqual(self.put_status('shipped').status_code, 200)
        self.theirs.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.theirs.status, 'processing')
        self.assertEqual(self.order.status, 'shipped')

    def test_single_store_order_keeps_whole_order_control(self):
        self.order.vendors.remove(self.other)
        self.theirs.delete()
        self.their_shipment.delete()
        self.assertEqual(self.put_status('delivered').status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'delivered')

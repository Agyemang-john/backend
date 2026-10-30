"""
order/tests_sourcing.py
Imported / made-to-order items: delivery dates, the order-time snapshot,
ship-by deadlines, seller progress steps, buyer cancellation and the daily checks.

Run with:  DB_HOST=localhost python manage.py test order.tests_sourcing --keepdb
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from order import sourcing
from order.fulfilment import late_orders, ship_by_for
from order.models import OrderProduct, Refund
from product import sourcing as product_sourcing
from product.models import DeliveryOption, Product, Variants
from vendor.tests_operations import OpsTestCase, vendor_client


class SourcingTestCase(OpsTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch('notification.utils.send_notification')
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)
        Product.objects.filter(pk=self.product.pk).update(
            fulfilment_type='imported', sourcing_min_days=28, sourcing_max_days=42)
        self.product.refresh_from_db()
        self.courier = DeliveryOption.objects.create(name='Standard', description='x', min_days=3, max_days=5)

    def make_sourced(self, line=None, **fields):
        line = line or self.line
        snap = sourcing.snapshot(self.product, None, self.courier, now=line.date_created)
        snap.update(fields)
        OrderProduct.objects.filter(pk=line.pk).update(**snap)
        line.refresh_from_db()
        return line

    def buyer(self):
        client = APIClient()
        client.force_authenticate(self.customer)
        return client


class ProductSideTests(SourcingTestCase):

    def test_window_and_variant_override(self):
        self.assertEqual(product_sourcing.window(self.product), (28, 42))
        in_stock = Variants(product=self.product, fulfilment_type='in_stock')
        self.assertEqual(product_sourcing.window(self.product, in_stock), (0, 0))
        inherits = Variants(product=self.product, fulfilment_type='')
        self.assertEqual(product_sourcing.window(self.product, inherits), (28, 42))

    def test_delivery_dates_include_sourcing(self):
        now = timezone.now()
        dates = self.courier.get_delivery_date_range(now, sourcing=(28, 42))
        self.assertIn((now + timedelta(days=31)).strftime('%b %d, %Y'), dates)
        self.assertIn((now + timedelta(days=47)).strftime('%b %d, %Y'), dates)

    def test_validation(self):
        self.assertIsNone(product_sourcing.validate('in_stock', 0, 0))
        self.assertIsNotNone(product_sourcing.validate('imported', 0, 0))
        self.assertIsNotNone(product_sourcing.validate('imported', 40, 30))
        self.assertIsNotNone(product_sourcing.validate('imported', 30, 99))

    def test_fast_delivery_filter(self):
        request = mock.Mock(GET={'fast': '1'})
        qs = product_sourcing.filter_fast_delivery(Product.objects.all(), request)
        self.assertFalse(qs.filter(pk=self.product.pk).exists())


class OrderSideTests(SourcingTestCase):

    def test_snapshot_promises_sourcing_plus_courier(self):
        line = self.make_sourced()
        self.assertEqual(line.sourcing_status, 'awaiting')
        self.assertEqual(line.promised_by, line.date_created.date() + timedelta(days=47))
        self.assertEqual(sourcing.snapshot(self.product, Variants(product=self.product, fulfilment_type='in_stock')), {})

    def test_ship_by_waits_for_sourcing(self):
        self.make_sourced()
        self.assertEqual(ship_by_for(self.order, self.vendor),
                         self.order.date_created + timedelta(days=2 + 42))
        # Old enough to be late for an in-stock order, but the import window is still open.
        type(self.order).objects.filter(pk=self.order.pk).update(date_created=timezone.now() - timedelta(days=5))
        OrderProduct.objects.filter(pk=self.line.pk).update(date_created=timezone.now() - timedelta(days=5))
        self.assertFalse(late_orders(self.vendor).exists())
        OrderProduct.objects.filter(pk=self.line.pk).update(date_created=timezone.now() - timedelta(days=60))
        type(self.order).objects.filter(pk=self.order.pk).update(date_created=timezone.now() - timedelta(days=60))
        self.assertTrue(late_orders(self.vendor).exists())

    def test_seller_steps_only_go_forward(self):
        self.make_sourced()
        url = f'/api/v1/vendor/orders/{self.order.pk}/sourcing/'
        client = vendor_client(self.owner)
        self.assertEqual(client.post(url, {'status': 'in_transit'}, format='json').status_code, 200)
        self.line.refresh_from_db()
        self.assertEqual(self.line.sourcing_status, 'in_transit')
        self.assertEqual(client.post(url, {'status': 'ordered'}, format='json').status_code, 400)
        self.assertEqual(client.post(url, {'status': 'bogus'}, format='json').status_code, 400)
        self.assertTrue(self.notify.called)

    def test_buyer_cancels_free_before_seller_orders(self):
        self.make_sourced()
        url = f'/api/v1/order/{self.order.pk}/items/{self.line.pk}/cancel/'
        res = self.buyer().post(url)
        self.assertEqual(res.status_code, 200, res.content)
        self.line.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.line.status, 'canceled')
        self.assertEqual(self.product.total_quantity, 52)  # restocked
        self.assertTrue(Refund.objects.filter(order_product=self.line, amount=200.0).exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'canceled')

    def test_buyer_cannot_cancel_once_ordered_and_on_time(self):
        self.make_sourced(sourcing_status='ordered')
        res = self.buyer().post(f'/api/v1/order/{self.order.pk}/items/{self.line.pk}/cancel/')
        self.assertEqual(res.status_code, 400)

    def test_late_line_becomes_cancellable_and_buyer_told_once(self):
        past = timezone.localdate() - timedelta(days=sourcing.GRACE_DAYS + 1)
        self.make_sourced(sourcing_status='ordered', promised_by=past)
        self.assertEqual(sourcing.cancel_reason(self.line), 'late')
        self.assertEqual(sourcing.notify_overdue(), 1)
        self.assertEqual(sourcing.notify_overdue(), 0)
        res = self.buyer().post(f'/api/v1/order/{self.order.pk}/items/{self.line.pk}/cancel/')
        self.assertEqual(res.status_code, 200)

    def test_other_buyers_cannot_cancel(self):
        self.make_sourced()
        from vendor.tests_operations import make_user
        client = APIClient()
        client.force_authenticate(make_user(9))
        self.assertEqual(client.post(f'/api/v1/order/{self.order.pk}/items/{self.line.pk}/cancel/').status_code, 404)

    def test_seller_reminder_after_order_window(self):
        self.make_sourced()
        self.assertEqual(sourcing.remind_sellers(), 0)
        OrderProduct.objects.filter(pk=self.line.pk).update(date_created=timezone.now() - timedelta(days=4))
        self.assertEqual(sourcing.remind_sellers(), 1)


class SellerTimelineTests(SourcingTestCase):
    """Dated steps, supplier details, deadlines, the Imported orders page and the daily digest."""

    def age(self, days):
        """Pretend the order was placed `days` ago (promise moves with it)."""
        then = timezone.now() - timedelta(days=days)
        type(self.order).objects.filter(pk=self.order.pk).update(date_created=then)
        OrderProduct.objects.filter(pk=self.line.pk).update(date_created=then)
        self.line.refresh_from_db()
        return self.make_sourced()

    def test_skipping_steps_still_dates_each_one(self):
        self.make_sourced()
        client = vendor_client(self.owner)
        res = client.post(f'/api/v1/vendor/orders/{self.order.pk}/sourcing/', {'status': 'arrived'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.line.refresh_from_db()
        self.assertIsNotNone(self.line.sourcing_ordered_at)
        self.assertIsNotNone(self.line.sourcing_in_transit_at)
        self.assertIsNotNone(self.line.sourcing_arrived_at)
        steps = sourcing.line_info(self.line)['steps']
        self.assertTrue(all(s['date'] for s in steps))

    def test_supplier_details_are_seller_only(self):
        self.make_sourced(sourcing_status='ordered')
        eta = (timezone.localdate() + timedelta(days=20)).isoformat()
        client = vendor_client(self.owner)
        url = f'/api/v1/vendor/orders/{self.order.pk}/sourcing/'
        res = client.post(url, {'supplier_eta': eta, 'supplier_tracking': 'LP00123CN'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.line.refresh_from_db()
        self.assertEqual(self.line.supplier_tracking, 'LP00123CN')
        self.assertEqual(self.line.sourcing_status, 'ordered')  # details alone don't move the step

        seller = sourcing.line_info(self.line, seller=True)
        self.assertEqual(seller['supplier_eta'], eta)
        buyer = sourcing.line_info(self.line)
        self.assertNotIn('supplier_tracking', buyer)
        self.assertNotIn('deadlines', buyer)

        past = (timezone.localdate() - timedelta(days=2)).isoformat()
        self.assertEqual(client.post(url, {'supplier_eta': past}, format='json').status_code, 400)
        self.assertEqual(client.post(url, {}, format='json').status_code, 400)

    def test_health_follows_the_current_step(self):
        line = self.make_sourced()
        self.assertEqual(sourcing.health(line), 'on_track')  # fresh order, 3 days to order it
        line = self.age(4)
        self.assertEqual(sourcing.health(line), 'overdue')   # still not ordered
        line = self.age(4)
        OrderProduct.objects.filter(pk=line.pk).update(sourcing_status='ordered')
        line.refresh_from_db()
        self.assertEqual(sourcing.health(line), 'on_track')  # 42-day window just started
        line = self.age(40)
        OrderProduct.objects.filter(pk=line.pk).update(sourcing_status='in_transit')
        line.refresh_from_db()
        self.assertEqual(sourcing.health(line), 'due_soon')  # should arrive within 2 days
        line = self.age(50)
        OrderProduct.objects.filter(pk=line.pk).update(sourcing_status='arrived')
        line.refresh_from_db()
        self.assertEqual(sourcing.health(line), 'late')      # past the promise to the buyer

    def test_imported_orders_page(self):
        self.make_sourced()
        client = vendor_client(self.owner)
        res = client.get('/api/v1/vendor/sourced-items/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['total'], 1)
        item = res.data['items'][0]
        self.assertEqual(item['order_id'], self.order.pk)
        s = item['sourcing']
        self.assertEqual(s['health'], 'on_track')
        self.assertEqual(s['next_action'], 'Order it from your supplier')
        self.assertEqual(s['day_number'], 1)
        self.assertEqual(s['total_days'], 47)
        self.assertEqual(s['days_left'], 47)
        self.assertEqual(client.get('/api/v1/vendor/sourced-items/?health=late,overdue').data['items'], [])

        # Handed to the courier: off the page.
        from order.models import Shipment
        shipment = Shipment.objects.create(order=self.order, vendor=self.vendor, status='in_transit')
        shipment.items.add(self.line)
        self.assertEqual(client.get('/api/v1/vendor/sourced-items/').data['total'], 0)

    def test_order_list_flags_imported_orders(self):
        self.make_sourced()
        res = vendor_client(self.owner).get('/api/v1/vendor/orders/')
        self.assertTrue(res.data['results'][0]['imported'])

    def test_action_center_keeps_open_imports_visible(self):
        from vendor.action_center import check_sourced_items
        self.make_sourced()
        self.assertEqual(check_sourced_items(self.vendor).key, 'sourced_in_progress')
        self.age(4)
        self.assertEqual(check_sourced_items(self.vendor).key, 'sourced_overdue')

    def test_digest_emails_only_when_things_get_worse(self):
        with mock.patch('vendor.tasks.send_sourcing_digest_email.delay') as email:
            self.make_sourced()
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(sourcing.remind_sellers(), 0)  # nothing needs action
            self.age(4)
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(sourcing.remind_sellers(), 1)
            self.assertEqual(email.call_count, 1)
            self.assertEqual(email.call_args.args[1][0]['level'], 'overdue')

            # Next day, same state: bell again, no second email.
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(sourcing.remind_sellers(), 1)
            self.assertEqual(email.call_count, 1)

            # It slips past the promise: email again.
            self.age(50)
            with self.captureOnCommitCallbacks(execute=True):
                sourcing.remind_sellers()
            self.assertEqual(email.call_count, 2)

            # Seller moves it on: the alert resets for the new step.
            client = vendor_client(self.owner)
            client.post(f'/api/v1/vendor/orders/{self.order.pk}/sourcing/', {'status': 'ordered'}, format='json')
            self.line.refresh_from_db()
            self.assertEqual(self.line.sourcing_alert_level, '')

    def test_digest_email_renders(self):
        from django.core import mail
        from vendor.tasks import send_sourcing_digest_email
        send_sourcing_digest_email(self.vendor.pk, [{
            'order_number': 'NM1', 'order_id': 1, 'item': 'Drone', 'action': 'Order it from your supplier',
            'when': 'was due Oct 06 (2 days ago)', 'level': 'overdue', 'promised_by': '2026-11-20', 'days_left': 21,
        }], '1 overdue')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Drone', mail.outbox[0].body)
        self.assertIn('/imported-orders', mail.outbox[0].body)

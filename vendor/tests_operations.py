"""
vendor/tests_operations.py
Seller operations: ledger & payouts, returns, delivery providers, action
centre, ship-by deadlines, review replies/reports, seller SKUs, plan gates.

Run with:  DB_HOST=localhost python manage.py test vendor.tests_operations --keepdb
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from order import returns as return_rules
from order.models import Order, OrderProduct, ReturnRequest, Shipment
from payments import ledger
from payments.ledger_models import LedgerEntry
from payments.models import Payout, SubscriptionPlan, VendorSubscription
from product.models import Category, Main_Category, Product, ProductReview, ReviewReport, Sub_Category, Variants
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor, VendorMember

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
    'RETURN_MIN_WINDOW_SELLER_FAULT_DAYS': 7,
    'SELLER_MIN_PAYOUT_AMOUNT': '10.00',
}
PASSWORD = 'Str0ng!Passw0rd'


def make_user(n, **extra):
    user = User.objects.create_user(f'User{n}', 'Test', f'ops{n}@example.com', f'+23324000{n:04d}', PASSWORD)
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now(), **extra)
    return User.objects.get(pk=user.pk)


def vendor_client(user):
    client = APIClient()
    client.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(user).access_token)
    client.credentials(HTTP_X_USER_TYPE='vendor')
    return client


@override_settings(**TEST_SETTINGS)
class OpsTestCase(TestCase):
    """Approved store on a plan with 10% commission and a 3-day hold, one product, one paid order."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(
            name='Ops Shop', user=self.owner, email='ops-shop@example.com', contact='+233550002222',
            is_approved=True, status='VERIFIED', handling_days=2, low_stock_threshold=5,
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Basic Test', tier='basic', price=Decimal('50.00'), commission_rate=Decimal('10.00'),
            payout_delay_days=3, max_team_members=3, can_export_reports=True,
        )
        VendorSubscription.objects.create(vendor=self.vendor, plan=self.plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))

        main = Main_Category.objects.create(title='Ops Main')
        cat = Category.objects.create(title='Ops Cat', main_category=main)
        self.sub = Sub_Category.objects.create(title='Ops Sub', category=cat)
        self.product = Product.objects.create(
            title='Ops Kettle', sub_category=self.sub, vendor=self.vendor, status='published',
            price=Decimal('100.00'), old_price=Decimal('120.00'), total_quantity=50, return_period_days=0,
        )
        self.customer = make_user(2)
        self.order = Order.objects.create(user=self.customer, total=Decimal('200.00'), is_ordered=True,
                                          status='processing')
        self.order.vendors.add(self.vendor)
        self.line = OrderProduct.objects.create(order=self.order, product=self.product, quantity=2,
                                                price=Decimal('100.00'), amount=Decimal('200.00'))

    def deliver(self, fulfilled_by='platform', delivered_at=None):
        shipment = Shipment.objects.create(order=self.order, vendor=self.vendor, status='delivered',
                                           fulfilled_by=fulfilled_by,
                                           delivered_at=delivered_at or timezone.now())
        shipment.items.set([self.line])
        from order.fulfilment import on_shipment_delivered
        with self.captureOnCommitCallbacks(execute=True):
            on_shipment_delivered(shipment)
        self.line.refresh_from_db()
        return shipment


class LedgerTests(OpsTestCase):

    def test_delivery_posts_sale_and_plan_commission_once(self):
        shipment = self.deliver()
        self.assertFalse(ledger.post_shipment_earnings(shipment.pk))  # idempotent
        entries = {e.entry_type: e for e in LedgerEntry.objects.filter(vendor=self.vendor)}
        self.assertEqual(entries[LedgerEntry.SALE].amount, Decimal('200.00'))
        self.assertEqual(entries[LedgerEntry.COMMISSION].amount, Decimal('-20.00'))
        self.assertEqual(entries[LedgerEntry.COMMISSION].rate, Decimal('10.00'))
        self.assertNotIn(LedgerEntry.DELIVERY_EARNING, entries)  # Negromart delivered → fee is ours
        self.assertEqual(self.line.status, 'delivered')
        self.assertIsNotNone(self.line.delivered_date)

    def test_earnings_held_then_available(self):
        self.deliver()
        summary = ledger.balance_summary(self.vendor)
        self.assertEqual(summary['pending'], Decimal('180.00'))
        self.assertEqual(summary['available'], Decimal('0.00'))
        LedgerEntry.objects.update(available_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(ledger.balance_summary(self.vendor)['available'], Decimal('180.00'))

    def test_payout_success_settles_and_failure_releases(self):
        self.deliver()
        LedgerEntry.objects.update(available_at=timezone.now() - timedelta(minutes=1))

        payout = ledger.reserve_payout(self.vendor)
        self.assertEqual(payout.amount, Decimal('180.00'))
        self.assertIsNone(ledger.reserve_payout(self.vendor))  # nothing left to reserve
        ledger.complete_payout(payout, success=False, error='bank down')
        self.assertEqual(ledger.balance_summary(self.vendor)['available'], Decimal('180.00'))

        payout = ledger.reserve_payout(self.vendor)
        ledger.complete_payout(payout, success=True, transaction_id='TRX1')
        summary = ledger.balance_summary(self.vendor)
        self.assertEqual(summary['available'], Decimal('0.00'))
        self.assertEqual(summary['paid_total'], Decimal('180.00'))
        self.assertTrue(LedgerEntry.objects.filter(entry_type=LedgerEntry.PAYOUT, amount=Decimal('-180.00')).exists())

    def test_seller_delivered_shipment_earns_the_delivery_fee(self):
        from order.service import FeeResult
        fee = FeeResult(total=Decimal('15.00'), dynamic_quotes={}, invalid_items=[])
        with mock.patch.object(Order, 'calculate_vendor_delivery_fee', return_value=fee):
            self.deliver(fulfilled_by='seller')
        self.assertTrue(LedgerEntry.objects.filter(entry_type=LedgerEntry.DELIVERY_EARNING,
                                                   amount=Decimal('15.00')).exists())

    def test_sweep_posts_deliveries_marked_by_bulk_update(self):
        shipment = Shipment.objects.create(order=self.order, vendor=self.vendor, status='in_transit')
        shipment.items.set([self.line])
        Shipment.objects.filter(pk=shipment.pk).update(status='delivered')  # like the admin bulk action
        from payments.tasks import sweep_delivered_shipments
        self.assertEqual(sweep_delivered_shipments(), 1)
        self.assertEqual(LedgerEntry.objects.filter(entry_type=LedgerEntry.SALE).count(), 1)

    def test_payouts_are_off_unless_enabled(self):
        from payments.tasks import run_seller_payouts
        self.assertEqual(run_seller_payouts(), 0)


class ReturnTests(OpsTestCase):

    def test_change_of_mind_needs_a_product_return_period(self):
        self.deliver()
        with self.assertRaises(return_rules.ReturnError):
            return_rules.open_return(customer=self.customer, line=self.line, reason='changed_mind')
        rr = return_rules.open_return(customer=self.customer, line=self.line, reason='damaged', quantity=1)
        self.assertEqual(rr.refund_amount, Decimal('100.00'))

    def test_window_closes(self):
        self.deliver(delivered_at=timezone.now() - timedelta(days=8))
        with self.assertRaises(return_rules.ReturnError):
            return_rules.open_return(customer=self.customer, line=self.line, reason='damaged')

    def test_not_before_delivery_and_not_someone_elses_order(self):
        with self.assertRaises(return_rules.ReturnError):
            return_rules.open_return(customer=self.customer, line=self.line, reason='damaged')
        self.deliver()
        with self.assertRaises(return_rules.ReturnError):
            return_rules.open_return(customer=make_user(9), line=self.line, reason='damaged')

    def test_full_flow_debits_seller_and_returns_commission_share(self):
        self.deliver()
        client = APIClient()
        client.force_authenticate(self.customer)
        res = client.post('/api/v1/order/returns/', {'order_product': self.line.pk, 'reason': 'damaged',
                                                     'quantity': 1}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        ref = res.data['reference']

        seller = vendor_client(self.owner)
        self.assertEqual(seller.post(f'/api/v1/vendor/returns/{ref}/receive/', {}, format='json').status_code, 400)
        self.assertEqual(seller.post(f'/api/v1/vendor/returns/{ref}/approve/', {}, format='json').status_code, 200)
        self.assertEqual(seller.post(f'/api/v1/vendor/returns/{ref}/receive/', {}, format='json').status_code, 200)

        rr = ReturnRequest.objects.get(reference=ref)
        return_rules.mark_refunded(rr, make_user(8, is_staff=True))
        refund = LedgerEntry.objects.get(entry_type=LedgerEntry.REFUND)
        back = LedgerEntry.objects.get(entry_type=LedgerEntry.COMMISSION_REFUND)
        self.assertEqual(refund.amount, Decimal('-100.00'))
        self.assertEqual(back.amount, Decimal('10.00'))  # half of the 20.00 commission

    def test_reject_requires_a_reason_and_one_open_return_per_line(self):
        self.deliver()
        rr = return_rules.open_return(customer=self.customer, line=self.line, reason='wrong_item')
        with self.assertRaises(return_rules.ReturnError):
            return_rules.open_return(customer=self.customer, line=self.line, reason='damaged')
        with self.assertRaises(return_rules.ReturnError):
            return_rules.reject(rr, self.owner, 'no')
        return_rules.reject(rr, self.owner, 'The photos show the correct item was delivered.')
        self.assertEqual(rr.status, ReturnRequest.STATUS_REJECTED)


class FulfilmentAndActionCenterTests(OpsTestCase):

    def test_late_orders_flagged_filtered_and_listed(self):
        Order.objects.filter(pk=self.order.pk).update(date_created=timezone.now() - timedelta(days=3))
        client = vendor_client(self.owner)
        res = client.get('/api/v1/vendor/orders/?filter=late')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['results'][0]['is_late'], True)
        keys = [i['key'] for i in client.get('/api/v1/vendor/action-center/').data['items']]
        self.assertEqual(keys[0], 'late_orders')
        self.assertIn('payout_method_missing', keys)

    def test_staff_do_not_see_finance_items(self):
        staff = make_user(3)
        VendorMember.objects.create(vendor=self.vendor, user=staff, role='staff')
        keys = [i['key'] for i in vendor_client(staff).get('/api/v1/vendor/action-center/').data['items']]
        self.assertNotIn('payout_method_missing', keys)
        self.assertIn('orders_to_ship', keys)

    def test_low_stock_and_operations_settings(self):
        Product.objects.filter(pk=self.product.pk).update(total_quantity=3)
        client = vendor_client(self.owner)
        keys = [i['key'] for i in client.get('/api/v1/vendor/action-center/').data['items']]
        self.assertIn('low_stock', keys)
        res = client.patch('/api/v1/vendor/operations-settings/', {'low_stock_threshold': 2}, format='json')
        self.assertEqual(res.status_code, 200)
        keys = [i['key'] for i in client.get('/api/v1/vendor/action-center/').data['items']]
        self.assertNotIn('low_stock', keys)

    def test_unknown_delivery_provider_webhook_is_rejected(self):
        res = APIClient().post('/api/v1/order/delivery/webhooks/nobody/', {}, format='json')
        self.assertEqual(res.status_code, 404)

    def test_platform_provider_books_shipments(self):
        res = vendor_client(self.owner).post(f'/api/v1/vendor/orders/{self.order.pk}/shipment/', {}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        shipment = Shipment.objects.get(order=self.order)
        self.assertEqual((shipment.provider, shipment.fulfilled_by), ('platform', 'platform'))
        self.assertTrue(shipment.tracking_number.startswith('NM'))


class ReviewTests(OpsTestCase):

    def setUp(self):
        super().setUp()
        self.review = ProductReview.objects.create(user=self.customer, product=self.product, rating=1,
                                                   review='Broke after one day.',
                                                   moderation_status=ProductReview.APPROVED)

    def test_sellers_cannot_hide_published_reviews(self):
        self.assertTrue(self.review.status)
        client = vendor_client(self.owner)
        client.patch(f'/api/v1/vendor/reviews/{self.review.pk}/', {'status': False}, format='json')
        self.review.refresh_from_db()
        self.assertTrue(self.review.status)

    def test_reply_and_report(self):
        client = vendor_client(self.owner)
        res = client.put(f'/api/v1/vendor/reviews/{self.review.pk}/reply/',
                         {'reply': 'Sorry about that. Please contact us for a replacement.'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.review.refresh_from_db()
        self.assertTrue(self.review.seller_reply.startswith('Sorry'))

        res = client.post(f'/api/v1/vendor/reviews/{self.review.pk}/report/', {'reason': 'fake'}, format='json')
        self.assertEqual(res.status_code, 201)
        res = client.post(f'/api/v1/vendor/reviews/{self.review.pk}/report/', {'reason': 'fake'}, format='json')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(ReviewReport.objects.count(), 1)


class PlanAndSkuTests(OpsTestCase):

    def test_exports_follow_the_plan(self):
        client = vendor_client(self.owner)
        res = client.get('/api/v1/vendor/orders/export.csv')
        self.assertEqual(res.status_code, 200)
        self.assertIn('Ops Kettle', b''.join(res.streaming_content).decode())

        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(can_export_reports=False)
        res = vendor_client(self.owner).get('/api/v1/vendor/finance/statement.csv')
        self.assertEqual(res.status_code, 403)

    def test_team_size_follows_the_plan(self):
        SubscriptionPlan.objects.filter(pk=self.plan.pk).update(max_team_members=1)
        with mock.patch('vendor.tasks.send_team_invitation_email.delay'):
            res = vendor_client(self.owner).post('/api/v1/vendor/team/invitations/',
                                                 {'email': 'new@example.com', 'role': 'staff'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'team_full')

    def test_seller_sku_unique_within_store(self):
        Product.objects.filter(pk=self.product.pk).update(seller_sku='KET-01')
        from vendor.product_serializers import ProductSerializer
        request = mock.Mock(user=self.owner, data={})
        serializer = ProductSerializer(context={'request': request})
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            serializer.validate_seller_sku('ket-01')
        self.assertEqual(serializer.validate_seller_sku('KET-02'), 'KET-02')

        Variants.objects.create(product=self.product, seller_sku='KET-RED', price=1)
        with self.assertRaises(ValidationError):
            serializer._check_variant_skus([{'seller_sku': 'ket-red'}])
        with self.assertRaises(ValidationError):
            serializer._check_variant_skus([{'seller_sku': 'A'}, {'seller_sku': 'a'}])

    def test_new_internal_skus_are_long(self):
        self.assertGreaterEqual(len(self.product.sku), 13)  # 'SKU' + 10 digits


class DeliveryIntegrityTests(OpsTestCase):

    def test_seller_cannot_mark_negromart_delivery_as_delivered(self):
        client = vendor_client(self.owner)
        res = client.post(f'/api/v1/vendor/orders/{self.order.pk}/shipment/', {'status': 'delivered'}, format='json')
        self.assertEqual(res.data['status'], 'label_created')
        sid = res.data['shipment_id']

        res = client.post(f'/api/v1/vendor/orders/{self.order.pk}/shipment/{sid}/event/',
                          {'status': 'delivered', 'description': 'Delivered', 'event_date': timezone.now().isoformat()},
                          format='json')
        self.assertEqual(res.status_code, 403)
        res = client.put(f'/api/v1/vendor/orders/{self.order.pk}/shipment/{sid}/', {'status': 'delivered'}, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertFalse(LedgerEntry.objects.exists())

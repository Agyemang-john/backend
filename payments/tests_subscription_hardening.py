"""
payments/tests_subscription_hardening.py
Loopholes closed in the subscription lifecycle (payments/subscription_lifecycle.py):
replayed payments, double renewals, stale quotes, product-limit resets,
dunning / grace / expiry, refunds, reminders.

Run with:  DB_HOST=localhost python manage.py test payments.tests_subscription_hardening --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from payments import entitlements, services
from payments import subscription_lifecycle as lifecycle
from payments.email_tasks import expire_old_subscriptions, process_subscription_renewals
from payments.models import PaymentTransaction, VendorSubscription
from payments.momo_services import _activate_momo_subscription
from payments.tests_subscriptions import PASSWORD, SubscriptionTestCase, fake_response
from product.models import Product
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor


def charge_ok(amount, id_=1):
    return fake_response({'status': True, 'data': {'status': 'success', 'id': id_, 'amount': amount, 'currency': 'GHS'}})


def charge_declined(reason='Insufficient funds'):
    return fake_response({'status': True, 'data': {'status': 'failed', 'gateway_response': reason}})


class ReplayAndActivationTests(SubscriptionTestCase):

    def paid_txn(self, ref, plan, amount, ttype='initial'):
        return PaymentTransaction.objects.create(
            vendor=self.vendor, transaction_type=ttype, amount=Decimal(amount), paystack_reference=ref,
            plan=plan, billing=plan.billing_cycle)

    @mock.patch('payments.services.requests.get')
    def test_old_reference_cannot_be_replayed_after_the_plan_ends(self, get):
        self.paid_txn('SUB-R1', self.pro, '300.00')
        get.return_value = fake_response(self.paystack_verify_data('SUB-R1', {'vendor_id': str(self.vendor.id)}, 30000))
        services.verify_and_activate('SUB-R1', vendor=self.vendor)
        sub = VendorSubscription.objects.get(vendor=self.vendor, status='active')

        # Plan ends; the seller re-opens the old verify link.
        VendorSubscription.objects.filter(pk=sub.pk).update(status='expired', end_date=timezone.now())
        services.verify_and_activate('SUB-R1', vendor=self.vendor)

        self.assertFalse(VendorSubscription.objects.filter(vendor=self.vendor, status='active').exists())
        self.assertEqual(VendorSubscription.objects.filter(vendor=self.vendor).count(), 1)

    def test_webhook_activates_when_the_seller_closed_the_tab(self):
        self.paid_txn('SUB-W1', self.pro, '300.00')
        event = {'reference': 'SUB-W1', 'id': 9, 'amount': 30000, 'currency': 'GHS',
                 'authorization': {}, 'customer': {'customer_code': 'CUS_x'}}
        services._webhook_charge_success(event)
        services._webhook_charge_success(event)  # Paystack retries

        self.assertEqual(VendorSubscription.objects.get(vendor=self.vendor, status='active').plan, self.pro)
        self.assertEqual(VendorSubscription.objects.filter(vendor=self.vendor).count(), 1)
        self.assertEqual(entitlements.plan_for_vendor(self.fresh_vendor()), self.pro)

    def test_payment_for_a_plan_already_held_is_parked_not_applied(self):
        current = self.subscribe(self.pro, days_left=20)
        txn = self.paid_txn('SUB-D1', self.pro, '300.00')  # second checkout for the same plan

        with self.assertRaises(lifecycle.PaymentParked):
            lifecycle.activate_paid_transaction(txn.pk, paid_pesewas=30000)

        txn.refresh_from_db()
        current.refresh_from_db()
        self.assertEqual(txn.status, 'review')
        self.assertEqual(current.status, 'active')
        self.assertAlmostEqual(current.end_date, self.now + timedelta(days=20), delta=timedelta(seconds=5))

    def test_stale_upgrade_quote_that_no_longer_covers_the_price_is_parked(self):
        # Quoted a cheap prorated upgrade, but the Basic plan lapsed before
        # paying: today Pro costs the full 300.
        txn = self.paid_txn('SUB-S1', self.pro, '20.00', ttype='upgrade')
        with self.assertRaises(lifecycle.PaymentParked):
            lifecycle.activate_paid_transaction(txn.pk, paid_pesewas=2000)
        self.assertFalse(VendorSubscription.objects.filter(vendor=self.vendor, status='active').exists())

    def test_underpaid_payment_is_refused(self):
        txn = self.paid_txn('SUB-U1', self.pro, '300.00')
        with self.assertRaises(lifecycle.ActivationRefused):
            lifecycle.activate_paid_transaction(txn.pk, paid_pesewas=100)
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'pending')

    def test_late_failure_event_does_not_undo_a_success(self):
        txn = self.paid_txn('SUB-F1', self.pro, '300.00')
        lifecycle.activate_paid_transaction(txn.pk, paid_pesewas=30000)
        services._webhook_charge_failed({'reference': 'SUB-F1', 'gateway_response': 'x'})
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'success')

    def test_only_one_live_subscription_per_store(self):
        self.subscribe(self.pro)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.subscribe(self.basic)


class MomoTests(SubscriptionTestCase):

    @mock.patch('payments.momo_services._post_charge')
    def test_manual_momo_renewal_extends_once_however_often_it_is_polled(self, post_charge):
        from payments.momo_services import initiate_momo_manual_payment
        sub = self.subscribe(self.pro, days_left=2)
        old_end = sub.end_date
        post_charge.return_value = (mock.Mock(status_code=200), {'status': True, 'data': {'status': 'pay_offline'}})
        with mock.patch('payments.momo_services.require_billing_profile', return_value=mock.Mock(email='b@x.com')):
            result = initiate_momo_manual_payment(self.vendor, phone='0241234567', provider='mtn')
        ref = result['reference']

        for _ in range(3):  # poll, poll, webhook
            _activate_momo_subscription(ref, paid_pesewas=30000, currency='GHS')

        sub.refresh_from_db()
        self.assertAlmostEqual(sub.end_date, old_end + timedelta(days=30), delta=timedelta(seconds=5))
        self.assertEqual(PaymentTransaction.objects.get(paystack_reference=ref).status, 'success')

    def test_poll_refuses_another_stores_reference(self):
        PaymentTransaction.objects.create(vendor=self.vendor, transaction_type='initial', amount=Decimal('300'),
                                          paystack_reference='MOMO-X1', plan=self.pro)
        other_user = User.objects.create_user('Oth', 'Er', 'other@example.com', '+233241009002', PASSWORD)
        User.objects.filter(pk=other_user.pk).update(is_active=True, email_verified_at=timezone.now())
        Vendor.objects.create(name='Other', user=other_user, email='o@example.com', contact='+233550009002',
                              is_approved=True, status='VERIFIED')
        api = APIClient()
        api.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(User.objects.get(pk=other_user.pk)).access_token)
        api.credentials(HTTP_X_USER_TYPE='vendor')
        res = api.get('/api/v1/payments/momo/status/', {'ref': 'MOMO-X1'})
        self.assertIn(res.status_code, (403, 404))


class RenewalTests(SubscriptionTestCase):

    def due_sub(self, **kw):
        return self.subscribe(self.pro, days_left=1, **kw)

    @mock.patch('payments.subscription_lifecycle.requests.post')
    def test_duplicate_tasks_charge_once(self, post):
        sub = self.due_sub()
        self.saved_card()
        post.return_value = charge_ok(30000)

        first = lifecycle.attempt_renewal(sub.pk)
        second = lifecycle.attempt_renewal(sub.pk)   # duplicate delivery

        self.assertEqual(first['status'], 'success')
        self.assertEqual(second['status'], 'not_due')
        self.assertEqual(post.call_count, 1)

    @mock.patch('payments.subscription_lifecycle.requests.get')
    @mock.patch('payments.subscription_lifecycle.requests.post')
    def test_unknown_outcome_is_settled_before_charging_again(self, post, get):
        sub = self.due_sub()
        self.saved_card()
        post.side_effect = ConnectionError('timeout')
        self.assertEqual(lifecycle.attempt_renewal(sub.pk)['status'], 'unknown')

        # Next attempt: Paystack says the first charge actually went through.
        VendorSubscription.objects.filter(pk=sub.pk).update(next_renewal_attempt_at=timezone.now())
        get.return_value = charge_ok(30000)
        post.side_effect = None
        result = lifecycle.attempt_renewal(sub.pk)

        self.assertEqual(result['status'], 'success')
        self.assertEqual(post.call_count, 1)  # never charged a second time
        sub.refresh_from_db()
        self.assertAlmostEqual(sub.end_date, self.now + timedelta(days=31), delta=timedelta(seconds=5))

    def test_second_payment_for_the_same_period_is_parked(self):
        sub = self.due_sub()
        mk = lambda ref: PaymentTransaction.objects.create(
            vendor=self.vendor, subscription=sub, transaction_type='renewal', amount=Decimal('300'),
            paystack_reference=ref, plan=self.pro, period_end=sub.end_date)
        a, b = mk('RNW-A'), mk('RNW-B')
        lifecycle.apply_renewal_payment(a.pk, paid_pesewas=30000)
        lifecycle.apply_renewal_payment(b.pk, paid_pesewas=30000)

        sub.refresh_from_db()
        b.refresh_from_db()
        self.assertAlmostEqual(sub.end_date, self.now + timedelta(days=31), delta=timedelta(seconds=5))
        self.assertEqual(b.status, 'review')

    @mock.patch('payments.subscription_lifecycle.requests.post')
    def test_failed_renewal_grace_then_free(self, post):
        sub = self.due_sub()
        self.saved_card()
        post.return_value = charge_declined()
        lifecycle.attempt_renewal(sub.pk)
        sub.refresh_from_db()
        self.assertEqual(sub.renewal_attempts, 1)
        self.assertEqual(sub.last_renewal_error, 'Insufficient funds')

        # Renewal date passes: past due, paid features kept during grace.
        later = self.now + timedelta(days=1, hours=1)
        with mock.patch('django.utils.timezone.now', return_value=later):
            lifecycle.sweep_lapsed()
            sub.refresh_from_db()
            self.assertEqual(sub.status, 'past_due')
            self.assertEqual(entitlements.plan_for_vendor(self.fresh_vendor()), self.pro)

        # Grace (3 days) over: Free.
        with mock.patch('django.utils.timezone.now', return_value=self.now + timedelta(days=5)):
            self.assertEqual(entitlements.plan_for_vendor(self.fresh_vendor()), self.free)  # even before the sweep
            lifecycle.sweep_lapsed()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'expired')

    @mock.patch('payments.subscription_lifecycle.requests.post')
    def test_retries_stop_after_the_configured_attempts(self, post):
        sub = self.due_sub()
        self.saved_card()
        post.return_value = charge_declined()
        for _ in range(10):
            VendorSubscription.objects.filter(pk=sub.pk).update(next_renewal_attempt_at=None)
            lifecycle.attempt_renewal(sub.pk)
        self.assertEqual(post.call_count, lifecycle.max_attempts())

    def test_scheduler_only_queues_due_subscriptions(self):
        due = self.due_sub()
        VendorSubscription.objects.create(vendor=Vendor.objects.create(
            name='Later', user=User.objects.create_user('L', 'L', 'l@example.com', '+233241009003', PASSWORD),
            email='l@example.com', contact='+233550009003'), plan=self.pro, status='active',
            end_date=self.now + timedelta(days=20))
        self.assertEqual(list(lifecycle.due_for_renewal_ids()), [due.pk])
        with mock.patch('payments.tasks.charge_vendor_for_renewal.delay') as delay:
            process_subscription_renewals()
        delay.assert_called_once_with(due.pk)

    def test_cancelled_plan_moves_to_free_at_end_without_grace(self):
        sub = self.subscribe(self.pro, days_left=1)
        sub.cancel()
        with mock.patch('django.utils.timezone.now', return_value=self.now + timedelta(days=1, hours=1)):
            expire_old_subscriptions()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'expired')

    def test_cancelling_a_past_due_plan_ends_it_now(self):
        sub = self.subscribe(self.pro, days_left=0)
        VendorSubscription.objects.filter(pk=sub.pk).update(
            status='past_due', end_date=self.now - timedelta(hours=1), grace_until=self.now + timedelta(days=2))
        services.cancel_subscription(self.vendor)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'expired')

    def test_trials_expire(self):
        VendorSubscription.objects.create(vendor=self.vendor, plan=self.pro, status='trial',
                                          end_date=self.now - timedelta(minutes=1))
        lifecycle.sweep_lapsed()
        self.assertEqual(entitlements.plan_for_vendor(self.fresh_vendor()), self.free)
        self.assertTrue(VendorSubscription.objects.filter(vendor=self.vendor, status='expired').exists())


class RefundAndReminderTests(SubscriptionTestCase):

    def test_full_refund_ends_the_plan_partial_does_not(self):
        txn = PaymentTransaction.objects.create(vendor=self.vendor, transaction_type='initial',
                                                amount=Decimal('300'), paystack_reference='SUB-RF', plan=self.pro)
        lifecycle.activate_paid_transaction(txn.pk, paid_pesewas=30000)

        services._webhook_refund_processed({'transaction_reference': 'SUB-RF', 'amount': 5000})
        self.assertEqual(entitlements.plan_for_vendor(self.fresh_vendor()), self.pro)

        services._webhook_refund_processed({'transaction_reference': 'SUB-RF', 'amount': 30000})
        self.assertEqual(entitlements.plan_for_vendor(self.fresh_vendor()), self.free)

    @mock.patch('payments.email_tasks.send_expiring_soon_email.delay')
    def test_expiry_reminder_is_sent_once_per_threshold(self, delay):
        sub = self.subscribe(self.pro, days_left=6)
        sub.cancel()
        lifecycle.send_upcoming_reminders()
        lifecycle.send_upcoming_reminders()   # second run the same day
        self.assertEqual(delay.call_count, 1)

        with mock.patch('django.utils.timezone.now', return_value=self.now + timedelta(days=4)):
            lifecycle.send_upcoming_reminders()   # 2 days left → 3-day reminder
        self.assertEqual(delay.call_count, 2)

    def test_store_without_payment_method_is_warned_before_renewal(self):
        from payments.email_tasks import send_payment_method_required_email
        self.subscribe(self.pro, days_left=6)   # auto-renew on, no card/MoMo
        lifecycle.send_upcoming_reminders()
        send_payment_method_required_email.delay.assert_called_once()
        self.assertTrue(send_payment_method_required_email.delay.call_args.kwargs['upcoming'])


class ProductLimitTests(SubscriptionTestCase):

    def setUp(self):
        super().setUp()
        self.free.max_products = 2
        self.free.save()
        for i in range(2):
            Product.objects.create(title=f'P{i}', vendor=self.vendor, price=Decimal('1'), total_quantity=1)

    def reserve(self):
        with transaction.atomic():
            entitlements.reserve_product_slots(self.fresh_vendor())

    def test_free_plan_limit_uses_real_count(self):
        with self.assertRaises(entitlements.ProductLimitReached):
            self.reserve()

    def test_reactivating_free_plan_does_not_reset_the_allowance(self):
        services.prepare_plan_change(self.vendor, self.free.id, 'monthly')
        services.prepare_plan_change(self.vendor, self.free.id, 'monthly')
        with self.assertRaises(entitlements.ProductLimitReached):
            self.reserve()

    def test_expired_paid_plan_limits_no_longer_apply(self):
        self.pro.max_products = 100
        self.pro.save()
        sub = self.subscribe(self.pro)
        self.reserve()  # Pro: fine
        VendorSubscription.objects.filter(pk=sub.pk).update(end_date=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(entitlements.ProductLimitReached):
            self.reserve()   # ended, even before the sweep marks it

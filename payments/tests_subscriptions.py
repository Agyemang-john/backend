"""
payments/tests_subscriptions.py
Subscription plan changes: cancelling keeps access until the period ends,
upgrades charge the prorated difference, downgrades wait for the next
renewal, and saved cards / MoMo renewals activate the right period.

Run with:  DB_HOST=localhost python manage.py test payments.tests_subscriptions --keepdb
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from payments import entitlements, services
from payments.email_tasks import expire_old_subscriptions
from payments.models import (
    PaymentTransaction, PaystackAuthorization, PaystackCustomer,
    SubscriptionPlan, VendorSubscription,
)
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}
PASSWORD = 'Str0ng!Passw0rd'

EMAIL_TASKS = (
    'send_subscription_confirmation_email', 'send_renewal_success_email',
    'send_payment_method_required_email', 'send_cancellation_email',
    'send_subscription_expired_email',
)


def fake_response(data):
    resp = mock.Mock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    resp.status_code = 200
    return resp


@override_settings(**TEST_SETTINGS)
class SubscriptionTestCase(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        patches = [mock.patch(f'payments.email_tasks.{name}.delay') for name in EMAIL_TASKS]
        patches += [mock.patch(t) for t in ('vendor.models.send_vendor_approval_email.delay',
                                            'vendor.models.send_vendor_sms.delay')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        user = User.objects.create_user('Sub', 'Tester', 'sub-owner@example.com', '+233241009001', PASSWORD)
        User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
        self.owner = User.objects.get(pk=user.pk)
        self.vendor = Vendor.objects.create(
            name='Sub Shop', user=self.owner, email='sub-shop@example.com', contact='+233550009001',
            is_approved=True, status='VERIFIED',
        )

        def plan(name, tier, cycle, price, **kw):
            return SubscriptionPlan.objects.create(
                name=name, tier=tier, billing_cycle=cycle, price=Decimal(price), **kw)

        self.free = plan('Free', 'free', 'monthly', '0.00', commission_rate=Decimal('15.00'))
        self.basic = plan('Basic', 'basic', 'monthly', '100.00', commission_rate=Decimal('12.00'))
        self.pro = plan('Pro', 'pro', 'monthly', '300.00', commission_rate=Decimal('8.00'))
        self.pro_yearly = plan('Pro Yearly', 'pro', 'yearly', '2880.00', commission_rate=Decimal('8.00'))
        self.now = timezone.now()

    def subscribe(self, plan, days_left=15, **kw):
        cycle_days = services.BILLING_DAYS[plan.billing_cycle]
        return VendorSubscription.objects.create(
            vendor=self.vendor, plan=plan, status='active',
            start_date=self.now - timedelta(days=cycle_days - days_left),
            end_date=self.now + timedelta(days=days_left), **kw)

    def fresh_vendor(self):
        return Vendor.objects.get(pk=self.vendor.pk)

    def saved_card(self):
        customer = PaystackCustomer.objects.create(
            vendor=self.vendor, customer_code='CUS_test', email=self.vendor.email)
        return PaystackAuthorization.objects.create(
            vendor=self.vendor, paystack_customer=customer, authorization_code='AUTH_test',
            card_type='visa', last4='4081', exp_month='12', exp_year='2030', is_default=True)

    def paystack_verify_data(self, reference, metadata, amount):
        return {'data': {
            'status': 'success', 'id': 42, 'reference': reference, 'amount': amount,
            'metadata': metadata,
            'customer': {'customer_code': 'CUS_test'},
            'authorization': {'reusable': True, 'authorization_code': 'AUTH_test', 'card_type': 'visa',
                              'last4': '4081', 'exp_month': '12', 'exp_year': '2030', 'bank': 'Test'},
        }}


class CancelTests(SubscriptionTestCase):

    def test_cancel_keeps_paid_plan_until_end_date(self):
        sub = self.subscribe(self.pro)
        services.cancel_subscription(self.vendor, reason='Too expensive')

        sub.refresh_from_db()
        self.assertEqual(sub.status, 'active')
        self.assertFalse(sub.auto_renew)
        self.assertTrue(sub.cancel_at_period_end)
        self.assertEqual(entitlements.plan_for_vendor(self.fresh_vendor()), self.pro)
        self.assertEqual(entitlements.commission_rate(self.fresh_vendor()), Decimal('8.00'))

    def test_store_moves_to_free_after_period_ends(self):
        sub = self.subscribe(self.pro)
        services.cancel_subscription(self.vendor)
        VendorSubscription.objects.filter(pk=sub.pk).update(end_date=self.now - timedelta(minutes=1))

        expire_old_subscriptions()

        sub.refresh_from_db()
        self.assertEqual(sub.status, 'expired')
        self.assertEqual(entitlements.plan_for_vendor(self.fresh_vendor()), self.free)

    def test_resume_undoes_cancellation(self):
        sub = self.subscribe(self.pro)
        services.cancel_subscription(self.vendor)
        services.resume_subscription(self.vendor)

        sub.refresh_from_db()
        self.assertTrue(sub.auto_renew)
        self.assertIsNone(sub.cancelled_at)

    def test_cannot_cancel_twice(self):
        self.subscribe(self.pro)
        services.cancel_subscription(self.vendor)
        with self.assertRaises(services.SubscriptionChangeError):
            services.cancel_subscription(self.vendor)


class QuoteTests(SubscriptionTestCase):

    def test_new_subscriber_pays_full_price(self):
        quote = services.quote_change(self.vendor, self.pro)
        self.assertEqual(quote['change_type'], 'initial')
        self.assertEqual(quote['amount_due'], Decimal('300.00'))

    def test_upgrade_same_cycle_charges_prorated_difference_and_keeps_renewal_date(self):
        sub = self.subscribe(self.basic, days_left=15)
        quote = services.quote_change(self.vendor, self.pro)

        self.assertEqual(quote['change_type'], 'upgrade')
        # Half the period left: half of (300 - 100).
        self.assertAlmostEqual(quote['amount_due'], Decimal('100.00'), delta=Decimal('0.10'))
        self.assertAlmostEqual(quote['credit'], Decimal('50.00'), delta=Decimal('0.10'))
        self.assertEqual(quote['period_end'], sub.end_date)

    def test_monthly_to_yearly_credits_unused_days_and_starts_new_period(self):
        self.subscribe(self.pro, days_left=15)
        quote = services.quote_change(self.vendor, self.pro_yearly)

        self.assertEqual(quote['change_type'], 'upgrade')
        self.assertAlmostEqual(quote['amount_due'], Decimal('2730.00'), delta=Decimal('0.10'))
        self.assertAlmostEqual(quote['period_end'], self.now + timedelta(days=365), delta=timedelta(minutes=1))

    def test_downgrade_waits_for_period_end(self):
        sub = self.subscribe(self.pro)
        quote = services.quote_change(self.vendor, self.basic)

        self.assertEqual(quote['change_type'], 'downgrade')
        self.assertEqual(quote['amount_due'], Decimal('0.00'))
        self.assertEqual(quote['effective_date'], sub.end_date)

    def test_yearly_to_monthly_is_a_downgrade(self):
        self.subscribe(self.pro_yearly, days_left=200)
        self.assertEqual(services.quote_change(self.vendor, self.pro)['change_type'], 'downgrade')

    def test_free_plan_subscription_is_treated_as_no_plan(self):
        VendorSubscription.objects.create(
            vendor=self.vendor, plan=self.free, status='active',
            end_date=self.now + timedelta(days=36500), auto_renew=False)
        self.assertEqual(services.quote_change(self.vendor, self.basic)['change_type'], 'initial')


class PlanChangeTests(SubscriptionTestCase):

    @mock.patch('payments.services.requests.post')
    def test_downgrade_is_scheduled_without_payment(self, post):
        sub = self.subscribe(self.pro)
        result = services.initiate_subscription(self.vendor, self.basic.id, 'monthly')

        post.assert_not_called()
        self.assertTrue(result['scheduled'])
        sub.refresh_from_db()
        self.assertEqual(sub.plan, self.pro)
        self.assertEqual(sub.scheduled_plan, self.basic)

    @mock.patch('payments.services.requests.post')
    def test_renewal_charges_and_switches_to_scheduled_plan(self, post):
        sub = self.subscribe(self.pro, days_left=3, scheduled_plan=self.basic)
        self.saved_card()
        post.return_value = fake_response({'data': {'status': 'success', 'id': 7}})
        old_end = sub.end_date

        services.charge_for_renewal(sub.id)

        self.assertEqual(post.call_args.kwargs['json']['amount'], 10000)
        sub.refresh_from_db()
        self.assertEqual(sub.plan, self.basic)
        self.assertIsNone(sub.scheduled_plan)
        # Extended from the old end date, not from today.
        self.assertAlmostEqual(sub.end_date, old_end + timedelta(days=30), delta=timedelta(seconds=5))

    @mock.patch('payments.services.requests.post')
    def test_upgrade_checkout_asks_paystack_for_prorated_amount(self, post):
        sub = self.subscribe(self.basic, days_left=15)
        post.return_value = fake_response({'status': True, 'data': {
            'authorization_url': 'https://checkout.paystack.com/x', 'access_code': 'ac'}})

        result = services.initiate_subscription(self.vendor, self.pro.id, 'monthly')

        payload = post.call_args.kwargs['json']
        self.assertEqual(result['transaction_type'], 'upgrade')
        self.assertAlmostEqual(payload['amount'], 10000, delta=10)
        self.assertEqual(payload['metadata']['period_end'], sub.end_date.isoformat())

    @mock.patch('payments.services.requests.get')
    def test_verify_keeps_the_quoted_renewal_date(self, get):
        old = self.subscribe(self.basic, days_left=15)
        ref = 'SUB-TEST-1'
        PaymentTransaction.objects.create(vendor=self.vendor, transaction_type='upgrade',
                                          amount=Decimal('100.00'), paystack_reference=ref)
        meta = {'vendor_id': str(self.vendor.id), 'plan_id': str(self.pro.id), 'billing': 'monthly',
                'transaction_type': 'upgrade', 'period_end': old.end_date.isoformat()}
        get.return_value = fake_response(self.paystack_verify_data(ref, meta, 10000))

        services.verify_and_activate(ref)

        new = VendorSubscription.objects.get(vendor=self.vendor, status='active')
        self.assertEqual(new.plan, self.pro)
        self.assertEqual(new.end_date, old.end_date)

    @mock.patch('payments.services.requests.get')
    @mock.patch('payments.services.requests.post')
    def test_saved_card_checkout_charges_card_directly(self, post, get):
        card = self.saved_card()
        post.return_value = fake_response({'data': {'status': 'success', 'id': 9}})

        def verify(url, **kw):
            ref = url.rsplit('/', 1)[-1]
            meta = post.call_args.kwargs['json']['metadata']
            return fake_response(self.paystack_verify_data(ref, meta, 30000))
        get.side_effect = verify

        result = services.charge_saved_card(self.vendor, self.pro.id, 'monthly', card.id)

        self.assertTrue(result['activated'])
        self.assertEqual(post.call_args.kwargs['json']['authorization_code'], 'AUTH_test')
        self.assertEqual(post.call_args.kwargs['json']['amount'], 30000)
        self.assertEqual(VendorSubscription.objects.get(vendor=self.vendor, status='active').plan, self.pro)

    @mock.patch('payments.services.requests.post')
    def test_declined_saved_card_is_reported(self, post):
        card = self.saved_card()
        post.return_value = fake_response({'data': {'status': 'failed', 'gateway_response': 'Declined.'}})

        with self.assertRaisesMessage(services.SubscriptionChangeError, 'Declined.'):
            services.charge_saved_card(self.vendor, self.pro.id, 'monthly', card.id)
        self.assertEqual(PaymentTransaction.objects.get(vendor=self.vendor).status, 'failed')

    def test_moving_to_free_requires_cancelling(self):
        self.subscribe(self.pro)
        with self.assertRaises(services.SubscriptionChangeError):
            services.initiate_subscription(self.vendor, self.free.id, 'monthly')

    def test_momo_renewal_webhook_extends_subscription(self):
        sub = self.subscribe(self.pro, days_left=2)
        old_end = sub.end_date
        PaymentTransaction.objects.create(vendor=self.vendor, subscription=sub, transaction_type='renewal',
                                          amount=Decimal('300.00'), paystack_reference='RNW-MM-1')

        services._webhook_charge_success({'reference': 'RNW-MM-1', 'id': 5})
        services._webhook_charge_success({'reference': 'RNW-MM-1', 'id': 5})  # retried webhook

        sub.refresh_from_db()
        self.assertAlmostEqual(sub.end_date, old_end + timedelta(days=30), delta=timedelta(seconds=5))
        self.assertEqual(PaymentTransaction.objects.get(paystack_reference='RNW-MM-1').status, 'success')


class SubscriptionApiTests(SubscriptionTestCase):

    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        self.api.credentials(HTTP_X_USER_TYPE='vendor')

    def test_quote_endpoint(self):
        self.subscribe(self.basic, days_left=15)
        res = self.api.get('/api/v1/payments/quote/', {'plan_id': self.pro.id, 'billing': 'monthly'})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['change_type'], 'upgrade')
        self.assertEqual(res.data['current_plan'], 'Basic')

    def test_cancel_resume_and_scheduled_change_endpoints(self):
        self.subscribe(self.pro)
        res = self.api.post('/api/v1/payments/cancel/', {'reason': 'testing'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['subscription']['cancel_at_period_end'])

        res = self.api.post('/api/v1/payments/resume/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.data['subscription']['cancel_at_period_end'])

        services.schedule_downgrade(self.vendor, self.basic)
        res = self.api.get('/api/v1/payments/plans/')
        self.assertEqual(res.data['current_subscription']['scheduled_plan']['name'], 'Basic')
        res = self.api.delete('/api/v1/payments/scheduled-change/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNone(res.data['subscription']['scheduled_plan'])

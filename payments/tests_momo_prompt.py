"""
payments/tests_momo_prompt.py
The MoMo approval prompt must reach the seller's phone: the number goes to
Paystack in Ghana's local form, and pressing Pay again sends a new prompt
instead of "resuming" a charge whose prompt has expired.

Run with:  DB_HOST=localhost python manage.py test payments.tests_momo_prompt --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase
from django.utils import timezone

from payments import momo_services
from payments.models import PaymentTransaction
from payments.tests_subscriptions import SubscriptionTestCase


def paystack(status_code, body):
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    return resp


class PhoneFormatTests(SimpleTestCase):

    def test_numbers_are_sent_in_local_ghana_form(self):
        for given in ('+233551234987', '233551234987', '0551234987', '055 123 4987', '+233 55-123-4987', '551234987'):
            with self.subTest(given=given):
                self.assertEqual(momo_services.paystack_phone(given), '0551234987')

    @mock.patch('payments.momo_services.requests.post')
    def test_charge_payload_has_no_plus_sign(self, post):
        post.return_value = paystack(200, {'status': True, 'data': {'status': 'pay_offline', 'display_text': 'Approve'}})
        momo_services._post_charge('MOMO-1', 30000, 'b@x.com', '+233241234567', 'mtn')
        sent = post.call_args.kwargs['json']['mobile_money']
        self.assertEqual(sent, {'phone': '0241234567', 'provider': 'mtn'})

    @mock.patch('payments.momo_services.requests.post')
    def test_airteltigo_falls_back_to_the_newer_code(self, post):
        post.side_effect = [
            paystack(400, {'status': False, 'message': 'Invalid provider'}),
            paystack(200, {'status': True, 'data': {'status': 'pay_offline'}}),
        ]
        resp, data = momo_services._post_charge('MOMO-2', 30000, 'b@x.com', '0271234567', 'tgo')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([c.kwargs['json']['mobile_money']['provider'] for c in post.call_args_list], ['tgo', 'atl'])

    @mock.patch('payments.momo_services.requests.post')
    def test_other_errors_are_not_retried(self, post):
        post.return_value = paystack(400, {'status': False, 'message': 'Insufficient balance'})
        momo_services._post_charge('MOMO-3', 30000, 'b@x.com', '0271234567', 'tgo')
        self.assertEqual(post.call_count, 1)


class ResumeWindowTests(SubscriptionTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        patcher = mock.patch('payments.momo_services.require_billing_profile', return_value=mock.Mock(email='b@x.com'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def pay(self):
        return momo_services.initiate_momo_charge(self.vendor, self.pro.id, self.pro.billing_cycle,
                                                  '+233241234567', 'mtn')

    @mock.patch('payments.momo_services._fetch_charge', return_value={'status': 'pay_offline'})
    @mock.patch('payments.momo_services.requests.post')
    def test_paying_again_soon_resumes_but_later_sends_a_new_prompt(self, post, fetch):
        post.return_value = paystack(200, {'status': True, 'data': {'status': 'pay_offline', 'display_text': 'Approve'}})
        first = self.pay()
        self.assertEqual(post.call_count, 1)

        again = self.pay()                      # a minute later: same prompt still on the phone
        self.assertEqual(again['reference'], first['reference'])
        self.assertTrue(again['resumed'])
        self.assertEqual(post.call_count, 1)

        PaymentTransaction.objects.filter(paystack_reference=first['reference']).update(
            created_at=timezone.now() - timedelta(minutes=10))
        later = self.pay()                      # the old prompt is gone: send a new one
        self.assertNotEqual(later['reference'], first['reference'])
        self.assertFalse(later['resumed'])
        self.assertEqual(post.call_count, 2)

    def test_old_pending_charges_are_not_resumed(self):
        old = PaymentTransaction.objects.create(
            vendor=self.vendor, transaction_type='initial', amount=Decimal('300'), status='pending',
            paystack_reference='MOMO-OLD', plan=self.pro)
        PaymentTransaction.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=2))
        self.assertIsNone(momo_services._get_most_recent_pending_momo(self.vendor))

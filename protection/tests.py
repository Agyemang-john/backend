"""
Negromart Guarantee claims: eligibility, seller offers, escalation, payout hold, ledger.

Run with:  DB_HOST=localhost python manage.py test protection.tests --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from order.models import Order, OrderProduct, Shipment
from payments.ledger import balance_summary, post_shipment_earnings, reserve_payout
from payments.ledger_models import LedgerEntry
from product.models import Brand, Category, Main_Category, Product, Sub_Category
from protection.models import Claim
from protection.services import run_deadlines
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


def make_user(n):
    user = User.objects.create_user(f'Clm{n}', 'Test', f'clm{n}@example.com', f'+23324800{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class ClaimTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(name='Clm Shop', user=self.owner, email='clm-shop@example.com',
                                            contact='+233550001111', is_approved=True, status='VERIFIED')
        main = Main_Category.objects.create(title='Clm Main')
        sub = Sub_Category.objects.create(title='Clm Sub', category=Category.objects.create(title='Clm Cat', main_category=main))
        self.product = Product.objects.create(title='Clm Speaker', sub_category=sub, vendor=self.vendor,
                                              status='published', price=Decimal('200'), total_quantity=5,
                                              brand=Brand.objects.create(title='Clm Brand'))
        self.buyer = make_user(2)
        self.order = Order.objects.create(user=self.buyer, total=Decimal('200'), is_ordered=True,
                                          order_number='ORD-CLM-1', payment_method='paystack')
        self.line = OrderProduct.objects.create(order=self.order, product=self.product, quantity=1,
                                                price=Decimal('200'), amount=Decimal('200'))
        self.shopper = APIClient()
        self.shopper.force_authenticate(self.buyer)
        self.seller = APIClient()
        self.seller.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        self.seller.credentials(HTTP_X_USER_TYPE='vendor')

    def deliver(self, days_ago=1):
        shipment = Shipment.objects.create(order=self.order, vendor=self.vendor, status='delivered',
                                           delivered_at=timezone.now() - timedelta(days=days_ago))
        shipment.items.set([self.line])
        post_shipment_earnings(shipment.pk)
        # Make the earnings payable now, to test the hold.
        LedgerEntry.objects.update(available_at=timezone.now() - timedelta(minutes=1))

    def open(self, reason='damaged', **extra):
        with self.captureOnCommitCallbacks(execute=True):
            return self.shopper.post('/api/v1/protection/claims/', {
                'order_product': self.line.pk, 'reason': reason,
                'details': 'The speaker arrived with a cracked case and no sound.', **extra}, format='json')

    def test_not_received_only_after_it_is_late(self):
        elig = self.shopper.get(f'/api/v1/protection/lines/{self.line.pk}/eligibility/').data
        self.assertFalse(elig['can_claim'])
        Order.objects.filter(pk=self.order.pk).update(date_created=timezone.now() - timedelta(days=11))
        elig = self.shopper.get(f'/api/v1/protection/lines/{self.line.pk}/eligibility/').data
        self.assertEqual([r['value'] for r in elig['reasons']], ['not_received'])
        self.assertEqual(self.open('damaged').status_code, 400)
        self.assertEqual(self.open('not_received').status_code, 201)

    def test_cod_cannot_claim_not_received(self):
        Order.objects.filter(pk=self.order.pk).update(payment_method='cash_on_delivery',
                                                      date_created=timezone.now() - timedelta(days=20))
        self.assertFalse(self.shopper.get(f'/api/v1/protection/lines/{self.line.pk}/eligibility/').data['can_claim'])

    def test_open_claim_holds_payout_and_full_offer_resolves(self):
        self.deliver()
        self.assertEqual(balance_summary(self.vendor)['available'], Decimal('200.00') - LedgerEntry.objects.get(
            entry_type='commission').amount * -1)
        res = self.open()
        self.assertEqual(res.status_code, 201, res.content)
        ref = res.data['reference']
        summary = balance_summary(self.vendor)
        self.assertEqual(summary['available'], Decimal('0.00'))
        self.assertGreater(summary['on_hold'], 0)
        self.assertIsNone(reserve_payout(self.vendor), "held lines aren't paid out")

        res = self.seller.post(f'/api/v1/protection/seller/claims/{ref}/offer/', {'amount': '200'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['status'], Claim.RESOLVED_BUYER)
        refund = LedgerEntry.objects.get(entry_type=LedgerEntry.REFUND)
        self.assertEqual(refund.amount, Decimal('-200.00'))
        self.assertTrue(LedgerEntry.objects.filter(entry_type=LedgerEntry.COMMISSION_REFUND).exists())

    def test_partial_offer_then_buyer_accepts(self):
        self.deliver()
        ref = self.open().data['reference']
        self.seller.post(f'/api/v1/protection/seller/claims/{ref}/offer/', {'amount': '50', 'note': 'Half back?'}, format='json')
        claim = Claim.objects.get(reference=ref)
        self.assertEqual(claim.status, Claim.SELLER_OFFERED)
        res = self.shopper.post(f'/api/v1/protection/claims/{ref}/accept/')
        self.assertEqual(res.data['status'], Claim.RESOLVED_BUYER)
        self.assertEqual(LedgerEntry.objects.get(entry_type=LedgerEntry.REFUND).amount, Decimal('-50.00'))

    def test_seller_silence_escalates(self):
        self.deliver()
        ref = self.open().data['reference']
        self.assertEqual(self.shopper.post(f'/api/v1/protection/claims/{ref}/escalate/').status_code, 400,
                         "seller still has time")
        Claim.objects.filter(reference=ref).update(seller_respond_by=timezone.now() - timedelta(minutes=1))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(run_deadlines(), 1)
        self.assertEqual(Claim.objects.get(reference=ref).status, Claim.ESCALATED)

    def test_not_received_refund_cancels_unearned_line(self):
        Order.objects.filter(pk=self.order.pk).update(date_created=timezone.now() - timedelta(days=11))
        ref = self.open('not_received').data['reference']
        from protection.services import resolve_for_buyer
        resolve_for_buyer(Claim.objects.get(reference=ref), Decimal('200'), None, 'Lost in transit.')
        self.line.refresh_from_db()
        self.assertEqual(self.line.status, 'canceled')
        self.assertFalse(LedgerEntry.objects.exists(), "never earned, nothing to debit")

    def test_one_active_claim_and_privacy(self):
        self.deliver()
        self.assertEqual(self.open().status_code, 201)
        self.assertEqual(self.open().status_code, 409)
        other = APIClient()
        other.force_authenticate(make_user(3))
        ref = Claim.objects.get().reference
        self.assertEqual(other.get(f'/api/v1/protection/claims/{ref}/').status_code, 404)

    def test_messages_both_sides(self):
        self.deliver()
        ref = self.open().data['reference']
        self.seller.post(f'/api/v1/protection/seller/claims/{ref}/messages/', {'text': 'Can you send a photo?'}, format='json')
        res = self.shopper.get(f'/api/v1/protection/claims/{ref}/')
        self.assertEqual([m['side'] for m in res.data['messages']], ['buyer', 'seller'])

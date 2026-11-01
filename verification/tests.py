"""
Seller KYC: ID number checks, submission, per-document review, approval, re-check triggers.

Run with:  DB_HOST=localhost python manage.py test verification.tests --keepdb
"""
import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor, VendorPaymentMethod
from verification import services
from verification.models import KycEvent, VerificationProfile

celery_app.conf.task_always_eager = True
MEDIA = tempfile.mkdtemp()

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
    'MEDIA_ROOT': MEDIA,
}
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


def doc(name='id.png'):
    return SimpleUploadedFile(name, PNG, content_type='image/png')


@override_settings(**TEST_SETTINGS)
class VerificationTests(TestCase):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owner = User.objects.create_user('Kofi', 'Mensah', 'kyc1@example.com', '+233251000001', 'Str0ng!Passw0rd')
        User.objects.filter(pk=self.owner.pk).update(is_active=True, email_verified_at=timezone.now())
        self.vendor = Vendor.objects.create(name='Kofi Mensah Ventures', user=self.owner, email='kyc-shop@example.com',
                                            contact='+233550003333', status='PENDING', vendor_type='non_student',
                                            business_type='sole_proprietor')
        # Not approved yet: the owner uses their account sign-in, like vendor/my-status.
        from rest_framework_simplejwt.tokens import RefreshToken
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.owner).access_token}')

    def test_ghana_card_format(self):
        self.assertEqual(services.clean_id_number('ghana_card', 'gha 123456789 0'), 'GHA-123456789-0')
        self.assertEqual(services.clean_id_number('ghana_card', 'GHA-123456789-0'), 'GHA-123456789-0')
        with self.assertRaises(services.KycError):
            services.clean_id_number('ghana_card', 'GHA-1234-0')
        with self.assertRaises(services.KycError):
            services.clean_business_reg('12-34')

    def test_pending_owner_sees_and_submits(self):
        res = self.api.get('/api/v1/verification/seller/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual({d['field']: d['state'] for d in res.data['documents']},
                         {'government_issued_id': 'missing', 'proof_of_address': 'missing'})
        res = self.api.put('/api/v1/verification/seller/', {
            'id_type': 'ghana_card', 'id_number': 'GHA-123456789-0',
            'government_issued_id': doc(), 'proof_of_address': doc('bill.png')}, format='multipart')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual({d['state'] for d in res.data['documents']}, {'pending'})
        self.assertEqual(res.data['id_number_masked'][:4], 'GHA-')
        self.assertNotIn('123456789', res.data['id_number_masked'])

    def test_bad_file_rejected(self):
        res = self.api.put('/api/v1/verification/seller/', {
            'government_issued_id': SimpleUploadedFile('x.exe', b'MZ\x90\x00', content_type='application/octet-stream')},
            format='multipart')
        self.assertEqual(res.status_code, 400)

    def test_reject_then_resubmit_then_approve(self):
        services.submit(self.vendor, self.owner, id_type='ghana_card', id_number='GHA-123456789-0',
                        files={'government_issued_id': doc(), 'proof_of_address': doc()})
        outcome = services.review(self.vendor, None, {'government_issued_id': ('rejected', 'Photo is blurry'),
                                                      'proof_of_address': ('accepted', '')})
        self.assertEqual(outcome, 'rejected')
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.status, 'REJECTED')
        res = self.api.get('/api/v1/verification/seller/')
        docs = {d['field']: d for d in res.data['documents']}
        self.assertEqual(docs['government_issued_id']['reason'], 'Photo is blurry')
        self.assertEqual(docs['proof_of_address']['state'], 'accepted')

        self.api.put('/api/v1/verification/seller/', {'government_issued_id': doc('new.png')}, format='multipart')
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.status, 'PENDING', "resubmission goes back to the queue")

        with self.assertRaises(services.KycError):
            services.review(self.vendor, None, {'government_issued_id': ('rejected', '')})
        self.assertEqual(services.review(self.vendor, None, {'government_issued_id': ('accepted', '')}), 'approved')
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.is_approved)
        self.assertEqual(self.vendor.status, 'VERIFIED')
        self.assertEqual(list(KycEvent.objects.filter(vendor=self.vendor).values_list('kind', flat=True))[:1], ['approved'])

    def test_payout_change_after_approval_flags_recheck(self):
        Vendor.objects.filter(pk=self.vendor.pk).update(is_approved=True, status='VERIFIED')
        self.vendor.refresh_from_db()
        method = VendorPaymentMethod(vendor=self.vendor, payment_method='bank', bank_name='GCB',
                                     bank_account_name='Kofi Mensah', bank_account_number='1234567890123')
        VendorPaymentMethod.objects.bulk_create([method])            # skip model full_clean (other required fields)
        from verification.signals import payout_method_saved
        payout_method_saved(VendorPaymentMethod, VendorPaymentMethod.objects.get(vendor=self.vendor), True)
        self.assertTrue(VerificationProfile.objects.get(vendor=self.vendor).needs_recheck)
        self.assertGreaterEqual(services.payout_name_match(self.vendor), 80)

    def test_staff_member_cannot_see_owner_kyc(self):
        from vendor.models import VendorMember
        staff = User.objects.create_user('Ama', 'Staff', 'kyc2@example.com', '+233251000002', 'Str0ng!Passw0rd')
        User.objects.filter(pk=staff.pk).update(is_active=True)
        VendorMember.objects.create(vendor=self.vendor, user=staff, role='staff', is_active=True)
        from rest_framework_simplejwt.tokens import RefreshToken
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(staff).access_token}')
        self.assertEqual(api.get('/api/v1/verification/seller/').status_code, 403)

    def test_approved_store_changes_need_seller_sign_in(self):
        Vendor.objects.filter(pk=self.vendor.pk).update(is_approved=True, status='VERIFIED')
        res = self.api.put('/api/v1/verification/seller/', {'government_issued_id': doc()}, format='multipart')
        self.assertEqual(res.status_code, 403)

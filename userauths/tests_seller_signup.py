"""
userauths/tests_seller_signup.py
Inline seller-account creation with email + phone codes, and phone
verification for existing customers.

Run with:  DB_HOST=localhost python manage.py test userauths.tests_seller_signup --keepdb
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from userauths import verification
from userauths.models import OTPRecord, User
from userauths.seller_signup_views import SIGNUP_COOKIE

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

PASSWORD = 'Str0ng!Passw0rd'


class CodeOutbox:
    """Captures codes handed to the send task instead of emailing/texting them."""

    def __init__(self):
        self.sent = []  # (recipient, code, channel)

    def __call__(self, recipient, code, channel, first_name=''):
        self.sent.append((recipient, str(code), channel))

    def last(self, channel):
        return next(code for _, code, ch in reversed(self.sent) if ch == channel)


@override_settings(**TEST_SETTINGS)
class SellerSignupFlowTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.client = APIClient()
        self.outbox = CodeOutbox()
        patches = [
            mock.patch('userauths.tasks.send_verification_code.delay', side_effect=self.outbox),
            mock.patch('userauths.captcha.verify_turnstile', return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def start(self, **overrides):
        payload = {
            'first_name': 'Ama', 'last_name': 'Mensah', 'email': 'ama@example.com',
            'phone': '+233241234567', 'password': PASSWORD, 'cf_turnstile_response': 'x',
            **overrides,
        }
        return self.client.post('/api/seller-signup/start/', payload, format='json')

    def test_full_flow_creates_verified_active_customer_and_signs_in(self):
        res = self.start()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['step'], 'verify_email')
        user = User.objects.get(email='ama@example.com')
        self.assertFalse(user.is_active)
        self.assertIn(SIGNUP_COOKIE, res.cookies)

        res = self.client.post('/api/seller-signup/verify-email/',
                               {'code': self.outbox.last('email')}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['step'], 'verify_phone')

        res = self.client.post('/api/seller-signup/verify-phone/',
                               {'code': self.outbox.last('sms')}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['step'], 'done')

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_email_verified and user.is_phone_verified)
        self.assertEqual(user.role, 'customer')
        # Signed in on the normal customer cookies; the signup cookie is gone.
        self.assertTrue(res.cookies['access'].value)
        self.assertTrue(res.cookies['refresh'].value)
        self.assertEqual(res.cookies[SIGNUP_COOKIE].value, '')

    def test_phone_step_cannot_be_skipped(self):
        self.start()
        res = self.client.post('/api/seller-signup/verify-phone/', {'code': '12345'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'email_unverified')

    def test_wrong_codes_burn_the_code_after_max_attempts(self):
        self.start()
        good = self.outbox.last('email')
        bad = '00000' if good != '00000' else '11111'
        for _ in range(verification.MAX_ATTEMPTS - 1):
            res = self.client.post('/api/seller-signup/verify-email/', {'code': bad}, format='json')
            self.assertEqual(res.data['code'], 'code_invalid')
        res = self.client.post('/api/seller-signup/verify-email/', {'code': bad}, format='json')
        self.assertEqual(res.data['code'], 'code_locked')
        # Even the right code no longer works; a new one must be requested.
        res = self.client.post('/api/seller-signup/verify-email/', {'code': good}, format='json')
        self.assertEqual(res.data['code'], 'code_expired')

    def test_resend_has_cooldown(self):
        self.start()
        res = self.client.post('/api/seller-signup/resend/', format='json')
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.data['code'], 'resend_too_soon')

    def test_active_account_must_sign_in_instead(self):
        User.objects.create_user('Kofi', 'Boateng', 'kofi@example.com', '+233200000001', PASSWORD)
        User.objects.filter(email='kofi@example.com').update(is_active=True)
        res = self.start(email='kofi@example.com', phone='+233200000002')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data['code'], 'account_exists')

    def test_abandoned_signup_resumes_only_with_original_password(self):
        self.start()
        fresh = APIClient()  # new browser, no signup cookie
        res = fresh.post('/api/seller-signup/start/', {
            'first_name': 'Ama', 'last_name': 'Mensah', 'email': 'AMA@example.com',
            'phone': '+233241234567', 'password': 'Wrong!Passw0rd', 'cf_turnstile_response': 'x',
        }, format='json')
        self.assertEqual(res.status_code, 409)

        res = fresh.post('/api/seller-signup/start/', {
            'first_name': 'Ama', 'last_name': 'Mensah', 'email': 'ama@example.com',
            'phone': '+233241234567', 'password': PASSWORD, 'cf_turnstile_response': 'x',
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(User.objects.filter(email__iexact='ama@example.com').count(), 1)

    def test_change_phone_during_phone_step(self):
        self.start()
        self.client.post('/api/seller-signup/verify-email/', {'code': self.outbox.last('email')}, format='json')
        OTPRecord.objects.update(created_at=timezone.now() - timedelta(minutes=2))  # past cooldown
        res = self.client.post('/api/seller-signup/change-phone/', {'phone': '+233 24 999 8888'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.outbox.sent[-1][0], '+233249998888')
        res = self.client.post('/api/seller-signup/verify-phone/', {'code': self.outbox.last('sms')}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(User.objects.get(email='ama@example.com').phone, '+233249998888')

    def test_login_codes_and_verification_codes_do_not_mix(self):
        user = User.objects.create_user('Esi', 'Owusu', 'esi@example.com', '+233200000003', PASSWORD)
        OTPRecord.create_for_user(user, 55555)  # a seller-login code
        result = verification.check_code(user, OTPRecord.PURPOSE_PHONE_VERIFY, '55555')
        self.assertFalse(result.ok)
        self.assertTrue(OTPRecord.objects.filter(user=user, purpose='vendor_login', is_used=False).exists())


@override_settings(**TEST_SETTINGS)
class ExistingCustomerPhoneVerificationTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.outbox = CodeOutbox()
        p = mock.patch('userauths.tasks.send_verification_code.delay', side_effect=self.outbox)
        p.start()
        self.addCleanup(p.stop)
        self.user = User.objects.create_user('Yaw', 'Asante', 'yaw@example.com', '+233200000010', PASSWORD)
        User.objects.filter(pk=self.user.pk).update(is_active=True, email_verified_at=timezone.now())
        self.user.refresh_from_db()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_verify_number_on_file(self):
        res = self.client.post('/api/phone/verification/send/', {}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        res = self.client.post('/api/phone/verification/confirm/', {'code': self.outbox.last('sms')}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_phone_verified)

    def test_new_number_only_saved_after_confirmation(self):
        self.client.post('/api/phone/verification/send/', {'phone': '+233200000099'}, format='json')
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone, '+233200000010')
        self.client.post('/api/phone/verification/confirm/', {'code': self.outbox.last('sms')}, format='json')
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone, '+233200000099')
        self.assertTrue(self.user.is_phone_verified)

    def test_editing_phone_elsewhere_clears_verification(self):
        # e.g. the customer profile form, which writes User.phone directly.
        User.objects.filter(pk=self.user.pk).update(phone_verified_at=timezone.now())
        user = User.objects.get(pk=self.user.pk)
        user.phone = '+233200000055'
        user.save()
        user.refresh_from_db()
        self.assertIsNone(user.phone_verified_at)

    def test_activation_by_any_path_marks_email_verified(self):
        pending = User.objects.create_user('New', 'Person', 'new@example.com', '+233200000066', PASSWORD)
        pending = User.objects.get(pk=pending.pk)
        self.assertIsNone(pending.email_verified_at)
        pending.is_active = True  # e.g. admin or djoser activation
        pending.save(update_fields=['is_active'])
        pending.refresh_from_db()
        self.assertIsNotNone(pending.email_verified_at)

    def test_cannot_claim_another_accounts_number(self):
        User.objects.create_user('Other', 'User', 'other@example.com', '+233200000077', PASSWORD)
        res = self.client.post('/api/phone/verification/send/', {'phone': '+233200000077'}, format='json')
        self.assertEqual(res.status_code, 400)

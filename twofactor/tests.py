"""
Two-factor: TOTP setup/verify, replay protection, recovery codes, admin enforcement, seller sign-in.

Run with:  DB_HOST=localhost python manage.py test twofactor.tests --keepdb
"""
import time
from unittest import mock

import pyotp
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from twofactor import services
from twofactor.models import TOTPDevice
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True
TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
    'ADMIN_2FA_REQUIRED': True,
}


def code_for(user, offset=0):
    secret = services._secret(TOTPDevice.objects.get(user=user))
    return pyotp.TOTP(secret).at(int(time.time()) + offset)


@override_settings(**TEST_SETTINGS)
class TwoFactorTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.user = User.objects.create_user('Two', 'Factor', 'tf1@example.com', '+233257000001', 'Str0ng!Passw0rd')
        User.objects.filter(pk=self.user.pk).update(is_active=True, email_verified_at=timezone.now())
        self.user.refresh_from_db()

    def enable(self, user=None):
        user = user or self.user
        services.start_setup(user)
        return services.confirm(user, code_for(user))

    def test_setup_confirm_and_secret_encrypted(self):
        secret, uri, svg = services.start_setup(self.user)
        self.assertIn('otpauth://totp/Negromart', uri)
        self.assertIn('<svg', svg)
        device = TOTPDevice.objects.get(user=self.user)
        self.assertNotIn(secret, device.secret_encrypted)
        with self.assertRaises(services.TwoFactorError):
            services.confirm(self.user, '000000')
        codes = services.confirm(self.user, code_for(self.user))
        self.assertEqual(len(codes), 10)
        self.assertTrue(services.is_enabled(self.user))

    def test_code_cannot_be_replayed(self):
        self.enable()
        code = code_for(self.user, 30)        # next step: not yet used by confirm
        self.assertTrue(services.verify(self.user, code))
        self.assertFalse(services.verify(self.user, code))

    def test_recovery_code_works_once(self):
        codes = self.enable()
        self.assertTrue(services.verify(self.user, codes[0].lower()))
        self.assertFalse(services.verify(self.user, codes[0]))
        self.assertEqual(services.recovery_left(self.user), 9)

    def test_lockout_after_wrong_codes(self):
        self.enable()
        for _ in range(5):
            self.assertFalse(services.verify(self.user, '123456'))
        with self.assertRaises(services.TwoFactorError):
            services.verify(self.user, code_for(self.user, 30))

    def test_admin_requires_second_factor(self):
        User.objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        self.client.force_login(User.objects.get(pk=self.user.pk))
        res = self.client.get('/dashboard/negromart/')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/dashboard/negromart/2fa/setup/', res['Location'])
        page = self.client.get(res['Location'])
        self.assertContains(page, '<svg')
        res = self.client.post('/dashboard/negromart/2fa/setup/', {'code': code_for(self.user), 'next': '/dashboard/negromart/'})
        self.assertContains(res, 'recovery codes')
        self.assertEqual(self.client.get('/dashboard/negromart/').status_code, 200)

        # New session: must verify.
        self.client.logout()
        self.client.force_login(User.objects.get(pk=self.user.pk))
        res = self.client.get('/dashboard/negromart/product/product/')
        self.assertIn('/dashboard/negromart/2fa/', res['Location'])
        res = self.client.post('/dashboard/negromart/2fa/', {'code': code_for(self.user, 30), 'next': '/dashboard/negromart/'})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.client.get('/dashboard/negromart/').status_code, 200)

    def test_seller_sign_in_uses_the_app(self):
        vendor = Vendor.objects.create(name='Tf Shop', user=self.user, email='tf-shop@example.com',
                                       contact='+233550006667', is_approved=True, status='VERIFIED')
        self.enable()
        api = APIClient()
        with mock.patch('userauths.captcha.verify_turnstile', return_value=True), \
                mock.patch('userauths.vendor_serializers.send_otp') as send_otp:
            res = api.post('/api/jwt/create/vendor/', {'email': 'tf1@example.com', 'password': 'Str0ng!Passw0rd'},
                           format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['method'], 'app')
        send_otp.delay.assert_not_called()
        self.assertEqual(api.post('/api/jwt/otp-verify/vendor/', {'otp': '000000'}, format='json').status_code, 400)
        res = api.post('/api/jwt/otp-verify/vendor/', {'otp': code_for(self.user, 30)}, format='json')
        self.assertEqual(res.status_code, 200, res.content)

    def test_seller_api_needs_seller_sign_in(self):
        Vendor.objects.create(name='Tf Shop 2', user=self.user, email='tf-shop2@example.com',
                              contact='+233550006668', is_approved=True, status='VERIFIED')
        seller = APIClient()
        seller.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.user).access_token)
        seller.credentials(HTTP_X_USER_TYPE='vendor')
        res = seller.post('/api/v1/2fa/', {'action': 'setup'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        res = seller.post('/api/v1/2fa/', {'action': 'confirm', 'code': code_for(self.user)}, format='json')
        self.assertEqual(len(res.data['recovery_codes']), 10)
        from rest_framework_simplejwt.tokens import RefreshToken
        shopper = APIClient()
        shopper.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')
        self.assertEqual(shopper.post('/api/v1/2fa/', {'action': 'disable', 'code': '123456'}, format='json').status_code, 403)


@override_settings(**TEST_SETTINGS)
class RecoveryAndResetTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('Rec', 'Over', 'tf9@example.com', '+233257000009', 'Str0ng!Passw0rd')
        User.objects.filter(pk=self.user.pk).update(is_active=True, is_staff=True, is_superuser=True)
        self.user.refresh_from_db()
        services.start_setup(self.user)
        self.old = services.confirm(self.user, code_for(self.user))

    def test_new_codes_replace_old(self):
        new = services.regenerate_recovery(self.user, code_for(self.user, 30))
        self.assertFalse(services.verify(self.user, self.old[0]))
        self.assertTrue(services.verify(self.user, new[0]))

    def test_admin_recovery_page(self):
        self.client.force_login(self.user)
        session = self.client.session
        session['admin_2fa_ok'] = True
        session.save()
        page = self.client.get('/dashboard/negromart/2fa/recovery/')
        self.assertContains(page, '10</b> unused')
        res = self.client.post('/dashboard/negromart/2fa/recovery/', {'code': code_for(self.user, 30)})
        self.assertContains(res, 'Save your recovery codes')

    def test_reset_command(self):
        from django.core.management import call_command
        call_command('reset_2fa', 'tf9@example.com')
        self.assertFalse(services.is_enabled(self.user))

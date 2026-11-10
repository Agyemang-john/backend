"""
userauths/tests_google_auth.py
Continue with Google, the email collisions around it, and switching sign-in
methods. Google's token check is mocked; everything else is real.

Run with:  DB_HOST=localhost python manage.py test userauths.tests_google_auth --keepdb
"""

from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from social_django.models import UserSocialAuth

from ecommerce.celery import app as celery_app
from userauths import google_auth
from userauths.models import User

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'GOOGLE_OAUTH_CLIENT_ID': 'test-client.apps.googleusercontent.com',
}
PASSWORD = 'Str0ng!Passw0rd'


def google_identity(sub='g-123', email='ama@gmail.com', first='Ama', last='Mensah'):
    return {'sub': sub, 'email': email, 'first_name': first, 'last_name': last}


@override_settings(**TEST_SETTINGS)
class GoogleAuthTests(TestCase):

    def setUp(self):
        celery_app.conf.task_always_eager = True
        from django.core.cache import cache
        cache.clear()  # throttle counters
        self.client = APIClient()
        self.client.credentials(HTTP_ORIGIN='http://localhost:3000')
        self.google = google_identity()
        patcher = mock.patch.object(google_auth, 'verify_google_credential', side_effect=lambda c: self.google)
        self.verify = patcher.start()
        self.addCleanup(patcher.stop)
        captcha = mock.patch('userauths.captcha.verify_turnstile', return_value=True)
        captcha.start()
        self.addCleanup(captcha.stop)

    # helpers
    def password_user(self, email='ama@gmail.com', phone='+233240000001', active=True):
        user = User.objects.create_user('Ama', 'Mensah', email, phone, PASSWORD)
        user.is_active = active
        user.save()
        return user

    def google_user(self, **kw):
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        self.assertEqual(res.data['status'], 'needs_profile')
        res = self.client.post('/api/auth/google/complete/', {
            'ticket': res.data['ticket'], 'first_name': 'Ama', 'last_name': 'Mensah',
            'phone': kw.get('phone', '+233240000009'),
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.client.cookies.clear()
        return User.objects.get(email=self.google['email'])

    def sign_in_as(self, user):
        self.client.force_authenticate(user)

    # ── New account with Google ───────────────────────────────────────────────

    def test_new_google_account_needs_phone_then_signs_in(self):
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        self.assertEqual(res.data['status'], 'needs_profile')
        self.assertEqual(res.data['profile']['first_name'], 'Ama')
        self.assertNotIn('access', res.cookies)

        res = self.client.post('/api/auth/google/complete/', {
            'ticket': res.data['ticket'], 'first_name': 'Ama', 'last_name': 'Mensah', 'phone': '+233 24 000 0009',
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIn('access', res.cookies)
        self.assertIn('refresh', res.cookies)
        user = User.objects.get(email='ama@gmail.com')
        self.assertTrue(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertIsNotNone(user.email_verified_at)
        self.assertEqual(user.phone, '+233240000009')
        self.assertTrue(UserSocialAuth.objects.filter(user=user, provider='google-oauth2', uid='g-123').exists())

        # Next time: straight in.
        self.client.cookies.clear()
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        self.assertEqual(res.data['status'], 'signed_in')
        self.assertIn('access', res.cookies)

    def test_complete_rejects_tampered_ticket_and_taken_phone(self):
        self.password_user(email='kofi@example.com', phone='+233240000001')
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        ticket = res.data['ticket']
        bad = self.client.post('/api/auth/google/complete/', {
            'ticket': ticket + 'x', 'first_name': 'A', 'last_name': 'M', 'phone': '+233240000009'}, format='json')
        self.assertEqual(bad.data['code'], 'ticket_invalid')
        taken = self.client.post('/api/auth/google/complete/', {
            'ticket': ticket, 'first_name': 'A', 'last_name': 'M', 'phone': '+233240000001'}, format='json')
        self.assertEqual(taken.status_code, 400)
        self.assertIn('phone', taken.data)
        # A link ticket can't be used to create an account.
        link_ticket = google_auth._ticket(self.google, 'link', 1)
        wrong = self.client.post('/api/auth/google/complete/', {
            'ticket': link_ticket, 'first_name': 'A', 'last_name': 'M', 'phone': '+233240000009'}, format='json')
        self.assertEqual(wrong.data['code'], 'ticket_invalid')

    def test_double_submit_doesnt_create_two_accounts(self):
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        body = {'ticket': res.data['ticket'], 'first_name': 'A', 'last_name': 'M', 'phone': '+233240000009'}
        self.assertEqual(self.client.post('/api/auth/google/complete/', body, format='json').status_code, 201)
        again = self.client.post('/api/auth/google/complete/', body, format='json')
        self.assertEqual(again.status_code, 409)
        self.assertEqual(User.objects.filter(email='ama@gmail.com').count(), 1)

    # ── Email already used ───────────────────────────────────────────────────

    def test_google_on_existing_password_account_requires_that_password(self):
        user = self.password_user()
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        self.assertEqual(res.data['status'], 'link_required')
        self.assertNotIn('access', res.cookies)

        wrong = self.client.post('/api/auth/google/link/', {'ticket': res.data['ticket'], 'password': 'nope'}, format='json')
        self.assertEqual(wrong.data['code'], 'wrong_password')
        self.assertFalse(UserSocialAuth.objects.filter(user=user).exists())

        ok = self.client.post('/api/auth/google/link/', {'ticket': res.data['ticket'], 'password': PASSWORD}, format='json')
        self.assertEqual(ok.data['status'], 'signed_in')
        self.assertIn('access', ok.cookies)
        self.assertTrue(UserSocialAuth.objects.filter(user=user, uid='g-123').exists())
        user.refresh_from_db()
        self.assertTrue(user.has_usable_password())  # both ways work now
        self.assertTrue(any('Google sign-in was added' in m.subject for m in mail.outbox))

    def test_link_locks_out_after_repeated_wrong_passwords(self):
        self.password_user()
        ticket = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json').data['ticket']
        # Per-IP throttling would stop this first; lockout covers many IPs.
        with mock.patch.object(google_auth.GoogleLinkView, 'throttle_classes', []):
            for _ in range(google_auth.MAX_FAILED_ATTEMPTS):
                self.client.post('/api/auth/google/link/', {'ticket': ticket, 'password': 'nope'}, format='json')
            res = self.client.post('/api/auth/google/link/', {'ticket': ticket, 'password': PASSWORD}, format='json')
        self.assertEqual(res.data['code'], 'locked')

    def test_pending_unactivated_signup_is_taken_over_and_old_password_dies(self):
        squatter = self.password_user(active=False, phone='+233240000001')
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        self.assertEqual(res.data['status'], 'needs_profile')
        res = self.client.post('/api/auth/google/complete/', {
            'ticket': res.data['ticket'], 'first_name': 'Ama', 'last_name': 'Mensah', 'phone': '+233240000009',
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        user = User.objects.get(email='ama@gmail.com')
        self.assertEqual(user.pk, squatter.pk)           # one account, not two
        self.assertTrue(user.is_active)
        self.assertFalse(user.check_password(PASSWORD))   # the old password no longer works
        self.assertEqual(user.phone, '+233240000009')

    def test_register_with_email_of_google_account_says_use_google(self):
        self.google_user()
        res = self.client.post('/api/register/', {
            'first_name': 'Ama', 'last_name': 'M', 'email': 'AMA@gmail.com', 'phone': '+233240000077',
            'password': PASSWORD, 'cf_turnstile_response': 'ok'}, format='json')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data['code'], 'email_google')
        self.assertEqual(User.objects.filter(email__iexact='ama@gmail.com').count(), 1)

    def test_register_with_email_of_password_account(self):
        self.password_user()
        res = self.client.post('/api/register/', {
            'first_name': 'Ama', 'last_name': 'M', 'email': 'ama@gmail.com', 'phone': '+233240000077',
            'password': PASSWORD, 'cf_turnstile_response': 'ok'}, format='json')
        self.assertEqual(res.data['code'], 'email_exists')

    def test_password_sign_in_on_google_account_explains_and_doesnt_count(self):
        user = self.google_user()
        res = self.client.post('/api/jwt/create/', {
            'email': 'ama@gmail.com', 'password': 'whatever', 'cf_turnstile_response': 'ok'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('google_account', res.data['code'])
        user.refresh_from_db()
        self.assertEqual(user.failed_login_attempts, 0)

    def test_suspended_account_cannot_use_google(self):
        user = self.google_user()
        User.objects.filter(pk=user.pk).update(is_suspended=True)
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        self.assertEqual(res.data['code'], 'account_suspended')
        self.assertNotIn('access', res.cookies)

    def test_same_email_different_google_account(self):
        self.google_user()
        self.google = google_identity(sub='g-OTHER')
        res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        self.assertEqual(res.data['code'], 'google_other_account')

    # ── Google → password ────────────────────────────────────────────────────

    def test_forgot_password_emails_google_only_account_and_marks_password_added(self):
        user = self.google_user()
        mail.outbox.clear()
        res = self.client.post('/api/users/reset_password/', {'email': 'ama@gmail.com'}, format='json')
        self.assertEqual(res.status_code, 204)
        self.assertEqual(len(mail.outbox), 1)

        from django.contrib.auth.tokens import default_token_generator
        from djoser.utils import encode_uid
        user.refresh_from_db()
        res = self.client.post('/api/users/reset_password_confirm/', {
            'uid': encode_uid(user.pk), 'token': default_token_generator.make_token(user),
            'new_password': 'N3w!Passw0rd', 're_new_password': 'N3w!Passw0rd'}, format='json')
        self.assertEqual(res.status_code, 204, res.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password('N3w!Passw0rd'))
        self.assertTrue(UserSocialAuth.objects.filter(user=user).exists())  # Google still works
        self.assertTrue(any('A password was added' in m.subject for m in mail.outbox))

    def test_google_only_cannot_disconnect_google(self):
        user = self.google_user()
        self.sign_in_as(user)
        res = self.client.post('/api/auth/sign-in-methods/google/disconnect/', {}, format='json')
        self.assertEqual(res.data['code'], 'last_method')
        self.assertTrue(UserSocialAuth.objects.filter(user=user).exists())

    def test_disconnect_google_after_password_needs_password(self):
        user = self.google_user()
        user.set_password(PASSWORD)
        user.save()
        self.sign_in_as(user)
        bad = self.client.post('/api/auth/sign-in-methods/google/disconnect/', {'password': 'nope'}, format='json')
        self.assertEqual(bad.data['code'], 'wrong_password')
        ok = self.client.post('/api/auth/sign-in-methods/google/disconnect/', {'password': PASSWORD}, format='json')
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(ok.data['google']['connected'])
        self.assertFalse(UserSocialAuth.objects.filter(user=user).exists())

    # ── Password → Google ────────────────────────────────────────────────────

    def test_connect_google_from_settings_needs_current_password(self):
        user = self.password_user()
        self.sign_in_as(user)
        bad = self.client.post('/api/auth/sign-in-methods/google/connect/', {'credential': 'x', 'password': 'no'}, format='json')
        self.assertEqual(bad.data['code'], 'wrong_password')
        ok = self.client.post('/api/auth/sign-in-methods/google/connect/', {'credential': 'x', 'password': PASSWORD}, format='json')
        self.assertTrue(ok.data['google']['connected'])
        self.assertTrue(ok.data['can_remove_password'])

    def test_google_account_already_used_elsewhere_cannot_be_connected(self):
        self.google_user()
        other = self.password_user(email='kofi@example.com', phone='+233240000002')
        self.sign_in_as(other)
        res = self.client.post('/api/auth/sign-in-methods/google/connect/', {'credential': 'x', 'password': PASSWORD}, format='json')
        self.assertEqual(res.data['code'], 'google_in_use')

    def test_remove_password_switches_to_google_only(self):
        user = self.password_user()
        self.sign_in_as(user)
        self.client.post('/api/auth/sign-in-methods/google/connect/', {'credential': 'x', 'password': PASSWORD}, format='json')
        res = self.client.post('/api/auth/sign-in-methods/password/remove/', {'password': PASSWORD}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data['has_password'])
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
        # Password sign-in now points at Google.
        self.client.force_authenticate(None)
        res = self.client.post('/api/jwt/create/', {
            'email': 'ama@gmail.com', 'password': PASSWORD, 'cf_turnstile_response': 'ok'}, format='json')
        self.assertIn('google_account', res.data['code'])

    def test_cannot_remove_password_without_google(self):
        user = self.password_user()
        self.sign_in_as(user)
        res = self.client.post('/api/auth/sign-in-methods/password/remove/', {'password': PASSWORD}, format='json')
        self.assertEqual(res.data['code'], 'last_method')
        user.refresh_from_db()
        self.assertTrue(user.has_usable_password())

    def test_seller_cannot_remove_password(self):
        user = self.password_user()
        self.sign_in_as(user)
        self.client.post('/api/auth/sign-in-methods/google/connect/', {'credential': 'x', 'password': PASSWORD}, format='json')
        with mock.patch.object(User, 'vendor_membership', new_callable=mock.PropertyMock, return_value=object()):
            res = self.client.post('/api/auth/sign-in-methods/password/remove/', {'password': PASSWORD}, format='json')
        self.assertEqual(res.data['code'], 'seller_needs_password')

    def test_google_switched_off_without_client_id(self):
        with override_settings(GOOGLE_OAUTH_CLIENT_ID=''):
            res = self.client.post('/api/auth/google/', {'credential': 'x'}, format='json')
        self.assertEqual(res.status_code, 503)


class VerifyCredentialTests(TestCase):
    """The real token check: only verified emails, only Google's issuers."""

    def _verify(self, info):
        with mock.patch('google.oauth2.id_token.verify_oauth2_token', return_value=info):
            return google_auth.verify_google_credential('token')

    def test_accepts_verified_google_token(self):
        got = self._verify({'iss': 'https://accounts.google.com', 'sub': '1', 'email': 'A@Gmail.com',
                            'email_verified': True, 'given_name': 'A'})
        self.assertEqual(got['email'], 'a@gmail.com')

    def test_rejects_unverified_email_and_foreign_issuer(self):
        with self.assertRaises(google_auth.GoogleTokenError):
            self._verify({'iss': 'accounts.google.com', 'sub': '1', 'email': 'a@x.com', 'email_verified': False})
        with self.assertRaises(google_auth.GoogleTokenError):
            self._verify({'iss': 'evil.example', 'sub': '1', 'email': 'a@x.com', 'email_verified': True})

    def test_bad_signature_is_an_error(self):
        with mock.patch('google.oauth2.id_token.verify_oauth2_token', side_effect=ValueError('bad')):
            with self.assertRaises(google_auth.GoogleTokenError):
                google_auth.verify_google_credential('token')

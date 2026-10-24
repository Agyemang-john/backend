"""
customer/tests_profile_security.py
A stolen shopper session must not be able to take the account over by moving
its email address (password resets go there) or phone number.

Run with:  DB_HOST=localhost python manage.py test customer.tests_profile_security --keepdb
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from userauths.models import Profile, User

PASSWORD = 'Str0ng!Passw0rd'
URL = '/api/v1/auth/user/profile/'


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class ProfileEmailChangeTests(TestCase):

    def setUp(self):
        cache.clear()
        self.sent = mock.patch('userauths.tasks.send_verification_code.delay').start()
        self.addCleanup(mock.patch.stopall)
        self.user = User.objects.create_user('Ama', 'M', 'ama@example.com', '+233200000101', PASSWORD)
        User.objects.filter(pk=self.user.pk).update(is_active=True, email_verified_at=timezone.now())
        Profile.objects.get_or_create(user=self.user)
        self.api = APIClient()
        self.api.force_authenticate(User.objects.get(pk=self.user.pk))

    def fresh(self):
        return User.objects.get(pk=self.user.pk)

    def test_email_change_without_password_is_refused(self):
        res = self.api.put(URL, {'email': 'thief@example.com'}, format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertIn('current_password', res.data)
        self.assertEqual(self.fresh().email, 'ama@example.com')

    def test_email_change_with_wrong_password_is_refused_and_capped(self):
        for _ in range(6):
            res = self.api.put(URL, {'email': 'thief@example.com', 'current_password': 'nope'},
                               format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertIn('Too many attempts', str(res.data))
        # Even the right password is refused once the cap is reached.
        res = self.api.put(URL, {'email': 'thief@example.com', 'current_password': PASSWORD},
                           format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.fresh().email, 'ama@example.com')

    def test_email_change_with_password_needs_new_verification(self):
        res = self.api.put(URL, {'email': 'New@Example.com', 'current_password': PASSWORD},
                           format='multipart')
        self.assertEqual(res.status_code, 200, res.data)
        user = self.fresh()
        self.assertEqual(user.email, 'new@example.com')
        self.assertIsNone(user.email_verified_at)
        self.sent.assert_called_once()

    def test_same_email_and_name_changes_need_no_password(self):
        res = self.api.put(URL, {'email': 'ama@example.com', 'first_name': 'Ama2'}, format='multipart')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.fresh().first_name, 'Ama2')

    def test_phone_cannot_be_changed_here(self):
        self.api.put(URL, {'user.phone': '+233200009999', 'user': '{"phone": "+233200009999"}'},
                     format='multipart')
        self.assertEqual(self.fresh().phone, '+233200000101')

    def test_email_taken_by_another_account(self):
        User.objects.create_user('K', 'O', 'kofi@example.com', '+233200000102', PASSWORD)
        res = self.api.put(URL, {'email': 'kofi@example.com', 'current_password': PASSWORD},
                           format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertIn('email', res.data)

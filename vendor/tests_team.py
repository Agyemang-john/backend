"""
vendor/tests_team.py
Store access through team membership (owner / admin / staff), invitations,
and the move away from User.role == 'vendor'.

Run with:  DB_HOST=localhost python manage.py test vendor.tests_team --keepdb
"""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.access import Capability, has_capability
from vendor.models import Vendor, VendorInvitation, VendorMember

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}

PASSWORD = 'Str0ng!Passw0rd'


def make_user(n, **extra):
    user = User.objects.create_user(f'User{n}', 'Test', f'user{n}@example.com', f'+23320000{n:04d}', PASSWORD)
    fields = {'is_active': True, 'email_verified_at': timezone.now(), **extra}
    User.objects.filter(pk=user.pk).update(**fields)
    user.refresh_from_db()
    return user


def vendor_client(user):
    """A client authenticated exactly like the seller dashboard (vendor cookie + header)."""
    client = APIClient()
    client.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(user).access_token)
    client.credentials(HTTP_X_USER_TYPE='vendor')
    return client


@override_settings(**TEST_SETTINGS)
class TeamAccessTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        for target in ('vendor.models.send_vendor_approval_email.delay',
                       'vendor.models.send_vendor_sms.delay',
                       'vendor.tasks.log_vendor_activity.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(
            name='Team Test Shop', user=self.owner, email='shop@example.com',
            contact='+233550001111', is_approved=True, status='VERIFIED',
        )
        # Team size comes from the subscription plan (payments/entitlements.py).
        from datetime import timedelta
        from payments.models import SubscriptionPlan, VendorSubscription
        plan = SubscriptionPlan.objects.create(name='Team Plan', tier='pro', max_team_members=10)
        VendorSubscription.objects.create(vendor=self.vendor, plan=plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))

    # ── helpers ──

    def add_member(self, user, role):
        return VendorMember.objects.create(vendor=self.vendor, user=user, role=role, added_by=self.owner)

    def invite(self, client, email, role='staff'):
        with mock.patch('vendor.tasks.send_team_invitation_email.delay') as send, \
                self.captureOnCommitCallbacks(execute=True):
            res = client.post('/api/v1/vendor/team/invitations/', {'email': email, 'role': role}, format='json')
        token = send.call_args[0][1] if send.called else None
        return res, token

    # ── membership replaces User.role ──

    def test_owner_membership_created_with_store_and_role_untouched(self):
        member = VendorMember.objects.get(vendor=self.vendor, user=self.owner)
        self.assertEqual(member.role, 'owner')
        self.assertTrue(self.owner.is_vendor)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.role, 'customer')  # approval no longer rewrites the role

    def test_rejecting_store_revokes_access_for_whole_team(self):
        staff = make_user(2)
        self.add_member(staff, 'staff')
        self.vendor.is_approved = False
        self.vendor.save()
        for user in (User.objects.get(pk=self.owner.pk), User.objects.get(pk=staff.pk)):
            self.assertFalse(user.is_vendor)
        res = vendor_client(self.owner).get('/api/v1/vendor/team/')
        self.assertIn(res.status_code, (401, 403))

    def test_staff_token_claims(self):
        staff = make_user(2)
        self.add_member(staff, 'staff')
        token = CustomVendorRefreshToken.for_user(User.objects.get(pk=staff.pk))
        self.assertEqual(token['role'], 'vendor')  # compat for the old seller proxy check
        self.assertTrue(token['is_vendor'])
        self.assertTrue(token['is_verified_vendor'])
        self.assertEqual(token['vendor_role'], 'staff')
        self.assertNotIn(Capability.MANAGE_FINANCE, token['vendor_capabilities'])

    # ── capabilities ──

    def test_role_capabilities(self):
        admin, staff = make_user(2), make_user(3)
        self.add_member(admin, 'admin')
        self.add_member(staff, 'staff')
        admin, staff = User.objects.get(pk=admin.pk), User.objects.get(pk=staff.pk)
        self.assertTrue(has_capability(self.owner, Capability.MANAGE_FINANCE))
        self.assertFalse(has_capability(admin, Capability.MANAGE_FINANCE))
        self.assertTrue(has_capability(admin, Capability.MANAGE_TEAM))
        self.assertTrue(has_capability(staff, Capability.MANAGE_ORDERS))
        self.assertFalse(has_capability(staff, Capability.MANAGE_STORE))

    def test_staff_endpoint_gates(self):
        staff = make_user(2)
        self.add_member(staff, 'staff')
        client = vendor_client(staff)
        self.assertEqual(client.get('/api/v1/vendor/team/').status_code, 200)
        self.assertEqual(client.get('/api/v1/vendor/orders/').status_code, 200)
        # Everyone may read the store profile (sidebar), only managers may edit it.
        self.assertEqual(client.get('/api/v1/vendor/about/management/').status_code, 200)
        self.assertEqual(client.get('/api/v1/vendor/payment-method/').status_code, 403)
        self.assertEqual(client.get('/api/v1/vendor/payouts/').status_code, 403)
        self.assertEqual(client.post('/api/v1/vendor/deletion-request/', {}, format='json').status_code, 403)

    # ── invitations ──

    def test_invite_accept_and_remove(self):
        invitee = make_user(5)
        owner_client = vendor_client(self.owner)

        res, token = self.invite(owner_client, 'USER5@example.com', 'staff')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(token)
        self.assertFalse(VendorInvitation.objects.filter(token_hash=token).exists())  # only the hash is stored

        lookup = APIClient().get('/api/v1/vendor/team/invitations/lookup/', {'token': token})
        self.assertEqual(lookup.data['status'], 'pending')
        self.assertTrue(lookup.data['account_exists'])
        self.assertEqual(lookup.data['store_name'], 'Team Test Shop')

        # Someone else signed in can't use the link.
        intruder = APIClient()
        intruder.force_authenticate(make_user(6))
        res = intruder.post('/api/v1/vendor/team/invitations/accept/', {'token': token}, format='json')
        self.assertEqual(res.data['code'], 'wrong_account')

        customer = APIClient()
        customer.force_authenticate(invitee)
        res = customer.post('/api/v1/vendor/team/invitations/accept/', {'token': token}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['role'], 'staff')
        # Single use.
        res = customer.post('/api/v1/vendor/team/invitations/accept/', {'token': token}, format='json')
        self.assertEqual(res.data['code'], 'accepted')

        invitee = User.objects.get(pk=invitee.pk)
        self.assertTrue(invitee.is_vendor)
        staff_client = vendor_client(invitee)
        self.assertEqual(staff_client.get('/api/v1/vendor/team/').status_code, 200)

        member = VendorMember.objects.get(vendor=self.vendor, user=invitee)
        res = owner_client.delete(f'/api/v1/vendor/team/members/{member.pk}/')
        self.assertEqual(res.status_code, 204)
        # Same (still unexpired) token is rejected on the very next request.
        self.assertIn(staff_client.get('/api/v1/vendor/team/').status_code, (401, 403))

    def test_admin_can_only_invite_staff_and_staff_cannot_invite(self):
        admin, staff = make_user(2), make_user(3)
        self.add_member(admin, 'admin')
        self.add_member(staff, 'staff')

        res, _ = self.invite(vendor_client(admin), 'new-admin@example.com', 'admin')
        self.assertEqual(res.status_code, 403)
        res, token = self.invite(vendor_client(admin), 'new-staff@example.com', 'staff')
        self.assertEqual(res.status_code, 201)
        res, _ = self.invite(vendor_client(staff), 'another@example.com', 'staff')
        self.assertEqual(res.status_code, 403)

    def test_owner_cannot_be_removed_and_staff_can_leave(self):
        staff = make_user(2)
        staff_member = self.add_member(staff, 'staff')
        owner_member = VendorMember.objects.get(vendor=self.vendor, role='owner')
        admin = make_user(3)
        self.add_member(admin, 'admin')

        res = vendor_client(admin).delete(f'/api/v1/vendor/team/members/{owner_member.pk}/')
        self.assertEqual(res.status_code, 403)
        res = vendor_client(staff).delete(f'/api/v1/vendor/team/members/{staff_member.pk}/')
        self.assertEqual(res.status_code, 204)

    def test_team_member_cannot_open_second_store(self):
        staff = make_user(2, phone_verified_at=timezone.now())
        self.add_member(staff, 'staff')
        client = APIClient()
        client.force_authenticate(staff)
        res = client.post('/api/v1/vendor/register/', {'name': 'My Own Shop'}, format='multipart')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data['code'], 'team_member')

    def test_store_application_requires_verified_phone(self):
        applicant = make_user(7)
        client = APIClient()
        client.force_authenticate(applicant)
        res = client.post('/api/v1/vendor/register/', {'name': 'New Shop'}, format='multipart')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['code'], 'phone_unverified')

    # ── seller login ──

    def test_staff_member_can_start_seller_login(self):
        staff = make_user(2)
        self.add_member(staff, 'staff')
        with mock.patch('userauths.captcha.verify_turnstile', return_value=True), \
                mock.patch('userauths.tasks.send_otp.delay'):
            res = APIClient().post('/api/jwt/create/vendor/', {
                'email': staff.email, 'password': PASSWORD, 'cf_turnstile_response': 'x',
            }, format='json')
            self.assertEqual(res.status_code, 200, res.data)
            outsider = make_user(9)
            res = APIClient().post('/api/jwt/create/vendor/', {
                'email': outsider.email, 'password': PASSWORD, 'cf_turnstile_response': 'x',
            }, format='json')
            self.assertEqual(res.status_code, 400)

"""
Audit log: diffs, masking, actor attribution (admin + API + system), read-only admin.

Run with:  DB_HOST=localhost python manage.py test audit.tests --keepdb
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from audit.models import AuditEntry
from ecommerce.celery import app as celery_app
from product.models import Brand, Category, Main_Category, Product, Sub_Category
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True
TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


@override_settings(**TEST_SETTINGS)
class AuditTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owner = User.objects.create_user('Aud', 'Owner', 'aud1@example.com', '+233256000001', 'Str0ng!Passw0rd')
        User.objects.filter(pk=self.owner.pk).update(is_active=True, email_verified_at=timezone.now())
        self.vendor = Vendor.objects.create(name='Aud Shop', user=self.owner, email='aud-shop@example.com',
                                            contact='+233550005556', is_approved=True, status='VERIFIED')
        main = Main_Category.objects.create(title='Aud Main')
        sub = Sub_Category.objects.create(title='Aud Sub', category=Category.objects.create(title='Aud Cat', main_category=main))
        self.product = Product.objects.create(title='Aud Lamp', sub_category=sub, vendor=self.vendor, status='published',
                                              price=Decimal('10'), total_quantity=3, brand=Brand.objects.create(title='Aud Brand'))
        AuditEntry.objects.all().delete()

    def test_only_tracked_field_changes_are_logged(self):
        self.product.views = 99
        self.product.save()
        self.assertFalse(AuditEntry.objects.exists(), "untracked field: no entry")
        self.product.price = Decimal('12.50')
        self.product.save()
        entry = AuditEntry.objects.get()
        self.assertEqual((entry.action, entry.via, entry.actor_label), ('update', 'system', 'system'))
        self.assertEqual(entry.changes, {'price': ['10.00', '12.50']})

    def test_seller_api_change_is_attributed(self):
        api = APIClient()
        api.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        api.credentials(HTTP_X_USER_TYPE='vendor')
        from vendor.models import VendorPaymentMethod
        res = api.get('/api/v1/vendor/my-status/')
        self.assertEqual(res.status_code, 200)
        # A direct save inside a request: attributed to the signed-in user via the middleware.
        from audit.services import _current_request

        class Req:
            path = '/api/v1/vendor/products/1/'
            user = self.owner
            request_id = 'req-123'
            META = {'REMOTE_ADDR': '10.0.0.9'}
            headers = {}
        token = _current_request.set(Req())
        try:
            self.product.status = 'disabled'
            self.product.save()
        finally:
            _current_request.reset(token)
        entry = AuditEntry.objects.get()
        self.assertEqual((entry.actor, entry.via, entry.request_id), (self.owner, 'api', 'req-123'))

    def test_sensitive_values_masked(self):
        from vendor.models import VendorPaymentMethod
        VendorPaymentMethod.objects.bulk_create([VendorPaymentMethod(vendor=self.vendor, payment_method='bank',
                                                                      bank_account_number='1234567890')])
        method = VendorPaymentMethod.objects.get(vendor=self.vendor)
        method.bank_account_number = '9999988888'
        from audit.registry import _post_save, _pre_save
        _pre_save(VendorPaymentMethod, method)
        VendorPaymentMethod.objects.filter(pk=method.pk).update(bank_account_number='9999988888')
        _post_save(VendorPaymentMethod, method, created=False)
        change = AuditEntry.objects.get(model='vendor.VendorPaymentMethod').changes['bank_account_number']
        self.assertEqual(change, ['•••••••890', '•••••••888'])

    def test_delete_and_admin_is_read_only(self):
        pk = self.product.pk
        self.product.delete()
        self.assertEqual(AuditEntry.objects.get(object_id=str(pk)).action, 'delete')
        staff = User.objects.create_user('Aud', 'Staff', 'aud2@example.com', '+233256000002', 'Str0ng!Passw0rd')
        User.objects.filter(pk=staff.pk).update(is_active=True, is_staff=True, is_superuser=True)
        self.client.force_login(User.objects.get(pk=staff.pk))
        entry = AuditEntry.objects.first()
        self.assertEqual(self.client.get('/dashboard/negromart/audit/auditentry/').status_code, 200)
        res = self.client.post(f'/dashboard/negromart/audit/auditentry/{entry.pk}/delete/', {'post': 'yes'})
        self.assertNotEqual(res.status_code, 302)
        self.assertTrue(AuditEntry.objects.filter(pk=entry.pk).exists())

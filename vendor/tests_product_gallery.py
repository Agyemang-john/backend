"""
vendor/tests_product_gallery.py
Product gallery photos on edit: the plan's max_images_per_product counts kept +
new photos, only adding is blocked, and removing every photo really removes them.

Run with:  DB_HOST=localhost python manage.py test vendor.tests_product_gallery --keepdb
"""
import json
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from payments.models import SubscriptionPlan, VendorSubscription
from product.models import Category, Main_Category, Product, ProductImages, Sub_Category
from vendor.models import Vendor
from vendor.tests_operations import TEST_SETTINGS, make_user, vendor_client
from vendor.tests_variant_gallery import photo

MEDIA = tempfile.mkdtemp(prefix='product-gallery-')
STORAGE = {
    'STORAGES': {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
    'MEDIA_ROOT': MEDIA,
}


@override_settings(**TEST_SETTINGS, **STORAGE)
class ProductGalleryEditTests(TestCase):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1})
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.plan = SubscriptionPlan.objects.create(name='Basic Test', tier='basic', price=Decimal('50'),
                                                    max_images_per_product=3)
        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(name='Photo Shop', user=self.owner, email='photo@example.com',
                                            contact='+233550005555', is_approved=True, status='VERIFIED')
        VendorSubscription.objects.create(vendor=self.vendor, plan=self.plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))
        main = Main_Category.objects.create(title='P Main')
        cat = Category.objects.create(title='P Cat', main_category=main)
        sub = Sub_Category.objects.create(title='P Sub', category=cat)
        self.product = Product.objects.create(title='Photo Kettle', sub_category=sub, vendor=self.vendor,
                                              status='published', variant='None', price=Decimal('80'), total_quantity=5)
        self.client = vendor_client(self.owner)

    def existing(self, n):
        return [ProductImages.objects.create(product=self.product, images=photo(f'old{i}.png')).id for i in range(n)]

    def patch(self, keep, new=0):
        data = {'keep_images': json.dumps(keep), 'images[]': [photo(f'new{i}.png') for i in range(new)]}
        return self.client.patch(f'/api/v1/vendor/products/{self.product.id}/', data, format='multipart')

    def test_edit_cannot_go_past_the_plan(self):
        kept = self.existing(2)
        res = self.patch(kept, new=2)                      # 2 kept + 2 new = 4 > 3
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data['error'], 'image_limit_exceeded')
        self.assertEqual(self.product.p_images.count(), 2, 'nothing changed')

    def test_edit_up_to_the_plan_and_swapping_photos_is_fine(self):
        kept = self.existing(3)
        res = self.patch(kept[:1], new=2)                  # remove 2, add 2 = 3
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.product.p_images.count(), 3)

    def test_after_a_downgrade_existing_photos_stay_and_edits_still_save(self):
        kept = self.existing(5)                            # more than the plan allows now
        self.assertEqual(self.patch(kept).status_code, 200)
        self.assertEqual(self.product.p_images.count(), 5)
        self.assertEqual(self.patch(kept, new=1).status_code, 403)

    def test_removing_every_photo_removes_them(self):
        self.existing(2)
        res = self.patch([])
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.product.p_images.count(), 0)

    def test_no_keep_list_keeps_photos(self):
        self.existing(2)
        res = self.client.patch(f'/api/v1/vendor/products/{self.product.id}/', {'title': 'Photo Kettle 2'}, format='multipart')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.product.p_images.count(), 2)

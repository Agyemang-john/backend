"""
vendor/tests_variant_gallery.py
Extra variant photos: plan gate, per-variant limit, ownership, re-encoding,
and removing/reordering through the product save (vendor/variant_gallery.py).

Run with:  DB_HOST=localhost python manage.py test vendor.tests_variant_gallery --keepdb
"""

import json
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from payments.models import SubscriptionPlan, VendorSubscription
from product.models import Category, Main_Category, Product, Sub_Category, VariantImage, Variants
from vendor.models import Vendor
from vendor.tests_operations import TEST_SETTINGS, make_user, vendor_client

MEDIA = tempfile.mkdtemp(prefix='variant-gallery-')
STORAGE = {
    'STORAGES': {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
    'MEDIA_ROOT': MEDIA,
}


def photo(name='p.png', fmt='PNG', size=(500, 500)):
    buffer = BytesIO()
    Image.new('RGB', size, (200, 30, 30)).save(buffer, format=fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f'image/{fmt.lower()}')


@override_settings(**TEST_SETTINGS, **STORAGE)
class VariantGalleryTests(TestCase):

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

        self.free = SubscriptionPlan.objects.create(name='Free Test', tier='free', price=Decimal('0'))
        self.basic = SubscriptionPlan.objects.create(
            name='Basic Test', tier='basic', price=Decimal('50.00'), max_images_per_variant=2,
        )
        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(
            name='Gallery Shop', user=self.owner, email='gallery@example.com', contact='+233550003333',
            is_approved=True, status='VERIFIED',
        )
        main = Main_Category.objects.create(title='G Main')
        cat = Category.objects.create(title='G Cat', main_category=main)
        self.sub = Sub_Category.objects.create(title='G Sub', category=cat)
        self.product = Product.objects.create(
            title='Gallery Shirt', sub_category=self.sub, vendor=self.vendor, status='published',
            variant='Size', price=Decimal('80.00'), total_quantity=5,
        )
        self.variant = Variants.objects.create(product=self.product, quantity=5, price=Decimal('80.00'))
        self.client = vendor_client(self.owner)

    def subscribe(self, plan):
        VendorSubscription.objects.create(vendor=self.vendor, plan=plan, status='active',
                                          end_date=timezone.now() + timedelta(days=30))

    def upload(self, variant=None, file=None):
        variant = variant or self.variant
        return self.client.post(f'/api/v1/vendor/variants/{variant.id}/images/',
                                {'image': file or photo()}, format='multipart')

    def test_free_plan_cannot_add_variant_photos(self):
        response = self.upload()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['error'], 'plan_upgrade_required')
        self.assertFalse(VariantImage.objects.exists())

    def test_plan_limit_per_variant(self):
        self.subscribe(self.basic)
        first, second, third = self.upload(), self.upload(), self.upload()
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(third.status_code, 403)
        self.assertEqual(third.data['error'], 'variant_image_limit_reached')
        self.assertEqual(str(third.data['limit']), '2')
        positions = list(self.variant.variantimage_set.values_list('position', flat=True))
        self.assertEqual(positions, [0, 1])

    def test_photo_is_reencoded_to_jpeg(self):
        self.subscribe(self.basic)
        response = self.upload(file=photo(size=(2400, 2400)))
        self.assertEqual(response.status_code, 201)
        stored = VariantImage.objects.get()
        self.assertTrue(stored.images.name.endswith('.jpg'))
        with Image.open(stored.images.path) as img:
            self.assertEqual(img.format, 'JPEG')
            self.assertLessEqual(max(img.size), 1800)

    def test_rejects_non_image(self):
        self.subscribe(self.basic)
        response = self.upload(file=SimpleUploadedFile('x.png', b'not an image', content_type='image/png'))
        self.assertEqual(response.status_code, 400)

    def test_cannot_upload_to_another_stores_variant(self):
        self.subscribe(self.basic)
        other_owner = make_user(2)
        other = Vendor.objects.create(name='Other', user=other_owner, email='o@example.com',
                                      contact='+233550004444', is_approved=True, status='VERIFIED')
        product = Product.objects.create(title='Other', sub_category=self.sub, vendor=other,
                                         variant='Size', price=Decimal('5'), total_quantity=1)
        variant = Variants.objects.create(product=product, quantity=1, price=Decimal('5'))
        self.assertEqual(self.upload(variant=variant).status_code, 404)

    def test_product_save_removes_and_reorders_gallery(self):
        self.subscribe(self.basic)
        a = self.upload().data['id']
        b = self.upload().data['id']
        variants = [{'id': self.variant.id, 'quantity': 5, 'price': '80.00', 'keep_gallery': [b]}]
        response = self.client.patch(f'/api/v1/vendor/products/{self.product.id}/',
                                     {'variants': json.dumps(variants)}, format='multipart')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([g['id'] for g in response.data['variants'][0]['gallery']], [b])
        self.assertFalse(VariantImage.objects.filter(id=a).exists())

        # Order follows keep_gallery.
        c = self.upload().data['id']
        variants[0]['keep_gallery'] = [c, b]
        response = self.client.patch(f'/api/v1/vendor/products/{self.product.id}/',
                                     {'variants': json.dumps(variants)}, format='multipart')
        self.assertEqual([g['id'] for g in response.data['variants'][0]['gallery']], [c, b])

    def test_new_photo_can_be_placed_before_existing_ones(self):
        self.subscribe(self.basic)
        a = self.upload().data['id']
        variants = [{'id': self.variant.id, 'quantity': 5, 'price': '80.00', 'keep_gallery': [None, a]}]
        self.client.patch(f'/api/v1/vendor/products/{self.product.id}/',
                          {'variants': json.dumps(variants)}, format='multipart')
        b = self.client.post(f'/api/v1/vendor/variants/{self.variant.id}/images/',
                             {'image': photo(), 'position': 0}, format='multipart').data['id']
        self.assertEqual(list(self.variant.variantimage_set.values_list('id', flat=True)), [b, a])

    def test_downgrade_keeps_photos_but_blocks_adding(self):
        self.subscribe(self.basic)
        self.upload()
        self.upload()
        VendorSubscription.objects.filter(vendor=self.vendor).update(status='expired')
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(self.variant.variantimage_set.count(), 2)

    def test_variant_without_keep_gallery_is_left_alone(self):
        self.subscribe(self.basic)
        self.upload()
        variants = [{'id': self.variant.id, 'quantity': 3, 'price': '80.00'}]
        self.client.patch(f'/api/v1/vendor/products/{self.product.id}/',
                          {'variants': json.dumps(variants)}, format='multipart')
        self.assertEqual(self.variant.variantimage_set.count(), 1)

    def test_new_variants_are_mapped_back_by_client_key(self):
        self.subscribe(self.basic)
        variants = [
            {'id': self.variant.id, 'quantity': 5, 'price': '80.00', 'client_key': 'old'},
            {'id': None, 'quantity': 2, 'price': '90.00', 'client_key': 'new-1'},
        ]
        response = self.client.patch(f'/api/v1/vendor/products/{self.product.id}/',
                                     {'variants': json.dumps(variants)}, format='multipart')
        self.assertEqual(response.status_code, 200, response.data)
        keys = response.data['variant_keys']
        self.assertEqual(keys['old'], self.variant.id)
        self.assertTrue(Variants.objects.filter(id=keys['new-1'], product=self.product).exists())

    def test_plan_summary_reports_variant_photo_limit(self):
        self.assertEqual(self.client.get('/api/v1/vendor/plan/').data['max_images_per_variant'], 0)
        self.subscribe(self.basic)
        self.assertEqual(self.client.get('/api/v1/vendor/plan/').data['max_images_per_variant'], 2)

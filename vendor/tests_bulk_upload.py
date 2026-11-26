"""
vendor/tests_bulk_upload.py
Bulk upload: spreadsheet and in-browser table, versions with any options,
photos from links or files, store defaults, drafts without photos.

Run with:  DB_HOST=localhost python manage.py test vendor.tests_bulk_upload --keepdb
"""

import csv
import io
import json
from decimal import Decimal
from io import BytesIO
from unittest import mock

from celery import current_app as celery_app
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from address.models import Country
from product.models import DeliveryOption, Product, ProductDeliveryOption, Variants
from vendor import bulk_upload, remote_images
from vendor.models import BulkUploadJob
from vendor.tests_operations import OpsTestCase, vendor_client

celery_app.conf.task_always_eager = True

BASE = '/api/v1/vendor/products/bulk-upload/'


def jpeg_bytes(size=600, color=(200, 30, 30)):
    buffer = BytesIO()
    Image.new('RGB', (size, size), color).save(buffer, format='JPEG')
    return buffer.getvalue()


def fake_fetch(url):
    if 'broken' in url:
        raise remote_images.RemoteImageError('The photo could not be downloaded (the website answered 404).')
    return ContentFile(jpeg_bytes(), name='fetched.jpg')


def to_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=bulk_upload.TEMPLATE_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, '') for k in bulk_upload.TEMPLATE_HEADERS})
    return out.getvalue().encode('utf-8')


class BulkTestCase(OpsTestCase):

    def setUp(self):
        super().setUp()
        self.plan.can_access_bulk_upload = True
        self.plan.max_products = 100
        self.plan.max_images_per_product = 5
        self.plan.save()
        self.client = vendor_client(self.owner)
        self.ghana, _ = Country.objects.get_or_create(name='Ghana', defaults={'code': 'GH'})
        self.nigeria, _ = Country.objects.get_or_create(name='Nigeria', defaults={'code': 'NG'})
        self.standard = DeliveryOption.objects.create(name='Standard', description='x', min_days=2, max_days=4, cost=10)
        self.express = DeliveryOption.objects.create(name='Express', description='x', min_days=1, max_days=1, cost=30)
        # The store's latest product sets the defaults: ships to Ghana by Express (default) + Standard.
        self.product.available_in_regions.set([self.ghana])
        ProductDeliveryOption.objects.create(product=self.product, delivery_option=self.express, default=True)
        ProductDeliveryOption.objects.create(product=self.product, delivery_option=self.standard, default=False)
        patcher = mock.patch('vendor.bulk_upload.fetch_photo', side_effect=fake_fetch)
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

    def row(self, **fields):
        return {'title': 'Cotton Tote', 'category': self.sub.slug, 'price': '45', 'stock': '30', 'weight_kg': '0.3',
                **fields}

    def upload_csv(self, rows, name='products.csv', raw=None):
        upload = SimpleUploadedFile(name, raw if raw is not None else to_csv(rows), content_type='text/csv')
        return self.client.post(BASE, {'file': upload}, format='multipart')


class SpreadsheetTests(BulkTestCase):

    def test_simple_product_gets_store_defaults_and_photos(self):
        res = self.upload_csv([self.row(image_url='https://img.example.com/tote.jpg',
                                        more_image_urls='https://img.example.com/2.jpg; https://img.example.com/3.jpg',
                                        compare_at_price='60', description='Strong bag.\n\nTwo pockets.',
                                        features='Cotton; Inner pocket', return_days='7', seller_sku='TOTE-1')])
        self.assertEqual(res.status_code, 202, res.content)      # photo links: background job
        job = BulkUploadJob.objects.get(pk=res.data['job_id'])
        self.assertEqual((job.status, job.success_count, job.failed_count), ('done', 1, 0), job.errors)

        product = Product.objects.get(vendor=self.vendor, title='Cotton Tote')
        self.assertEqual(product.status, 'in_review')
        self.assertTrue(product.image.name.endswith('.jpg'))
        self.assertEqual(product.p_images.count(), 2)
        self.assertEqual((product.price, product.old_price, product.total_quantity), (Decimal('45'), Decimal('60'), 30))
        self.assertEqual(product.weight, 0.3)
        self.assertEqual(product.description, '<p>Strong bag.</p><p>Two pockets.</p>')
        self.assertEqual(product.features, '<ul><li>Cotton</li><li>Inner pocket</li></ul>')
        self.assertEqual(list(product.available_in_regions.all()), [self.ghana])
        options = list(product.productdeliveryoption_set.order_by('-default', 'id').values_list('delivery_option__name', 'default'))
        self.assertEqual(options, [('Express', True), ('Standard', False)])
        self.assertEqual(job.results[0]['status'], 'in_review')

    def test_versions_with_any_options(self):
        rows = [
            self.row(title='Protein Shake', price='120', stock='25', weight_kg='0.6', condition='grocery',
                     option1_name='Flavor', option1_value='Vanilla', option2_name='Size', option2_value='500 g',
                     seller_sku='PS-1'),
            self.row(title='Protein Shake', category='', price='200', stock='10', option1_name='Flavor',
                     option1_value='Vanilla', option2_name='Size', option2_value='1 kg', seller_sku='PS-2'),
            self.row(title='Protein Shake', category='', price='120', stock='0', option1_name='Flavor',
                     option1_value='Chocolate', option2_name='Size', option2_value='500 g'),
        ]
        res = self.upload_csv(rows)
        self.assertEqual(res.status_code, 201, res.content)      # no links: done in the request
        product = Product.objects.get(vendor=self.vendor, title='Protein Shake')
        self.assertEqual(product.variant, 'Options')
        self.assertEqual(product.variation_axes, [{'key': 'flavor', 'label': 'Flavour'}, {'key': 'size', 'label': 'Size'}])
        self.assertEqual(product.product_type, 'grocery')
        self.assertEqual((product.price, product.total_quantity), (Decimal('120'), 35))
        self.assertEqual(product.variants.count(), 3)
        big = product.variants.get(seller_sku='PS-2')
        self.assertEqual((big.options, big.price, big.quantity), ({'flavor': 'Vanilla', 'size': '1 kg'}, Decimal('200'), 10))
        self.assertEqual(product.status, 'draft', 'no photo: a draft to finish')
        self.assertEqual(res.data['draft_count'], 1)
        self.assertIn('No photo', res.data['products'][0]['warnings'][0])

    def test_first_template_columns_still_work(self):
        legacy = ('title,price,old_price,sub_category_slug,product_type,total_quantity,weight,variant,size_names,'
                  'variant_prices,variant_quantities\n'
                  f'Old Tee,50,60,{self.sub.slug},new,0,0.2,Size,S;M,50;55,3;4\n').encode()
        res = self.upload_csv([], raw=legacy)
        self.assertEqual(res.status_code, 201, res.content)
        tee = Product.objects.get(vendor=self.vendor, title='Old Tee')
        self.assertEqual(tee.variant, 'Size')
        self.assertEqual(sorted(tee.variants.values_list('options__size', 'price', 'quantity')),
                         [('M', Decimal('55'), 4), ('S', Decimal('50'), 3)])

    def test_row_errors_are_reported_and_good_rows_saved(self):
        rows = [
            self.row(title='Good One'),
            self.row(title='No Weight', weight_kg=''),
            self.row(title='Bad Category', category='nope'),
            self.row(title='Ops Kettle'),                                   # already in the store
            self.row(title='Bad Country', ships_to='GH; Atlantis'),
            self.row(title='Dup', option1_name='Size', option1_value='M'),
            self.row(title='Dup', option1_name='Size', option1_value='m'),
            self.row(title='Same Title Twice'),
            self.row(title='Same Title Twice'),
            self.row(title='Not A Link', image_url='photo.jpg'),
        ]
        res = self.upload_csv(rows)
        self.assertEqual(res.status_code, 207, res.content)
        self.assertEqual(res.data['success_count'], 1)
        problems = {e['title']: e['errors'] for e in res.data['errors']}
        self.assertIn('weight_kg', problems['No Weight'])
        self.assertIn('category', problems['Bad Category'])
        self.assertIn('already have', problems['Ops Kettle']['title'])
        self.assertIn('Atlantis', problems['Bad Country']['ships_to'])
        self.assertIn('same version', problems['Dup']['options'])
        self.assertIn('option columns', problems['Same Title Twice']['title'])
        self.assertIn('http', problems['Not A Link']['image_url'])
        self.assertEqual(res.data['errors'][1]['row'], 4, 'spreadsheet row numbers (header is row 1)')

    def test_example_rows_are_skipped_and_template_downloads(self):
        res = self.client.get(f'{BASE}template/')
        self.assertEqual(res.status_code, 200)
        text = res.content.decode('utf-8-sig')
        self.assertTrue(text.startswith('title,category,price'))
        upload = SimpleUploadedFile('t.csv', text.encode('utf-8'), content_type='text/csv')
        res = self.client.post(BASE, {'file': upload}, format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertIn('example rows', res.data['error'])

    def test_excel_files_and_encodings(self):
        res = self.upload_csv([], name='products.xlsx', raw=b'PK\x03\x04')
        self.assertIn('CSV', res.data['error'])
        raw = to_csv([self.row(title='Café Mug')]).decode('utf-8').encode('cp1252')
        res = self.upload_csv([], raw=raw)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(Product.objects.filter(title='Café Mug').exists())
        semicolons = f'title;category;price;stock;weight_kg\nSemi Bag;{self.sub.slug};10;1;0.1\n'.encode()
        self.assertEqual(self.upload_csv([], raw=semicolons).status_code, 201)

    def test_broken_photo_link_saves_a_draft_with_a_warning(self):
        res = self.upload_csv([self.row(image_url='https://img.example.com/broken.jpg')])
        job = BulkUploadJob.objects.get(pk=res.data['job_id'])
        self.assertEqual(job.success_count, 1)
        self.assertEqual(job.results[0]['status'], 'draft')
        self.assertTrue(any('404' in w for w in job.results[0]['warnings']))

    def test_plan_limits(self):
        self.plan.max_products = 2      # the store already has one product
        self.plan.save()
        res = self.upload_csv([self.row(title='A'), self.row(title='B')])
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['remaining_slots'], 1)


class TableTests(BulkTestCase):

    def test_table_with_attached_photos_and_versions(self):
        products = [
            {'title': 'Scented Candle', 'category': self.sub.slug, 'price': '30', 'stock': '5', 'weight_kg': '0.4',
             'ships_to': 'GH;NG', 'delivery_methods': 'Standard'},
            {'rows': [
                {'title': 'Body Lotion', 'category': self.sub.slug, 'price': '25', 'stock': '3', 'weight_kg': '0.3',
                 'option1_name': 'Scent', 'option1_value': 'Lavender'},
                {'title': 'Body Lotion', 'price': '27', 'stock': '2', 'option1_name': 'Scent', 'option1_value': 'Citrus'},
            ]},
        ]
        files = {
            'photos_0': [SimpleUploadedFile('a.jpg', jpeg_bytes(), 'image/jpeg'),
                         SimpleUploadedFile('b.png', jpeg_bytes(), 'image/png')],
            'version_photo_1_1': SimpleUploadedFile('c.jpg', jpeg_bytes(color=(0, 200, 0)), 'image/jpeg'),
        }
        res = self.client.post(f'{BASE}direct/', {'products': json.dumps(products), **files}, format='multipart')
        self.assertEqual(res.status_code, 201, res.content)
        candle = Product.objects.get(title='Scented Candle')
        self.assertEqual(candle.status, 'in_review')
        self.assertEqual(candle.p_images.count(), 1)
        self.assertEqual(set(candle.available_in_regions.values_list('code', flat=True)), {'GH', 'NG'})
        self.assertEqual(list(candle.productdeliveryoption_set.values_list('delivery_option__name', flat=True)), ['Standard'])
        lotion = Product.objects.get(title='Body Lotion')
        self.assertEqual(lotion.status, 'in_review', 'a version photo is enough to go to review')
        citrus = lotion.variants.get(options__scent='Citrus')
        self.assertNotEqual(citrus.image.name, 'product.jpg')

    def test_tiny_or_fake_photo_is_refused_with_a_warning(self):
        products = [{'title': 'Tiny', 'category': self.sub.slug, 'price': '5', 'stock': '1', 'weight_kg': '0.1'}]
        files = {'photos_0': SimpleUploadedFile('t.jpg', jpeg_bytes(size=100), 'image/jpeg')}
        res = self.client.post(f'{BASE}direct/', {'products': json.dumps(products), **files}, format='multipart')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['products'][0]['status'], 'draft')
        self.assertTrue(any('too small' in w for w in res.data['products'][0]['warnings']))

    def test_meta_lists_what_the_page_needs(self):
        res = self.client.get(f'{BASE}meta/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['required_columns'], ['title', 'category', 'price', 'stock', 'weight_kg'])
        self.assertEqual([c['code'] for c in res.data['defaults']['ships_to']], ['GH'])
        self.assertEqual(res.data['defaults']['delivery_methods'][0]['name'], 'Express')
        self.assertEqual(res.data['max_images_per_product'], 5)


class DraftFinishTests(BulkTestCase):

    def form(self, product, **extra):
        return {'title': product.title, 'slug': product.slug, 'available_in_regions': [self.ghana.pk], 'price': '45',
                'total_quantity': '3', 'sub_category': self.sub.pk, 'brand': '', 'variant': 'None', 'weight': '0.3',
                'product_type': 'new', **extra}

    def test_adding_a_photo_sends_a_draft_to_review(self):
        self.upload_csv([self.row(title='Needs Photo')])
        draft = Product.objects.get(title='Needs Photo')
        self.assertEqual(draft.status, 'draft')
        url = f'/api/v1/vendor/products/{draft.pk}/'

        self.assertEqual(self.client.put(url, self.form(draft), format='multipart').status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft', 'still no photo')

        photo = SimpleUploadedFile('p.jpg', jpeg_bytes(), 'image/jpeg')
        res = self.client.put(url, self.form(draft, image=photo), format='multipart')
        self.assertEqual(res.status_code, 200, res.content)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'in_review')

    def test_sellers_cannot_publish_their_own_product(self):
        res = self.client.put(f'/api/v1/vendor/products/{self.product.pk}/',
                              self.form(self.product, status='published'), format='multipart')
        self.assertEqual(res.status_code, 200, res.content)
        Product.objects.filter(pk=self.product.pk).update(status='in_review')
        self.client.put(f'/api/v1/vendor/products/{self.product.pk}/', self.form(self.product, status='published'),
                        format='multipart')
        self.product.refresh_from_db()
        self.assertEqual(self.product.status, 'in_review')

    def test_editing_a_rejected_product_sends_it_back_to_review(self):
        Product.objects.filter(pk=self.product.pk).update(status='rejected', review_note='Photo has a watermark.')
        res = self.client.put(f'/api/v1/vendor/products/{self.product.pk}/', self.form(self.product), format='multipart')
        self.assertEqual(res.status_code, 200, res.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.status, 'in_review')
        self.assertEqual(self.product.review_note, 'Photo has a watermark.', 'kept until the next decision')

    def test_editing_a_live_product_keeps_it_live(self):
        Product.objects.filter(pk=self.product.pk).update(status='published')
        self.client.put(f'/api/v1/vendor/products/{self.product.pk}/', self.form(self.product, price='50'),
                        format='multipart')
        self.product.refresh_from_db()
        self.assertEqual(self.product.status, 'published')


class SameAsProductFormTests(BulkTestCase):
    """A product added in bulk ends up like the same product added with the product form."""

    def setUp(self):
        super().setUp()
        from attributes.models import AttributeDefinition, AttributeOption
        self.material = AttributeDefinition.objects.create(name='Material', type=AttributeDefinition.CHOICE)
        self.material.sub_categories.add(self.sub)
        self.cotton = AttributeOption.objects.create(definition=self.material, value='Cotton', slug='cotton')
        AttributeOption.objects.create(definition=self.material, value='Leather', slug='leather')
        self.capacity = AttributeDefinition.objects.create(name='Capacity', type=AttributeDefinition.NUMBER, unit='L')
        self.capacity.sub_categories.add(self.sub)
        self.waterproof = AttributeDefinition.objects.create(name='Waterproof', type=AttributeDefinition.BOOLEAN,
                                                             required=True)
        self.waterproof.sub_categories.add(self.sub)
        from attributes import services
        services.clear_definitions_cache()
        self.plan.max_images_per_variant = 2
        self.plan.save()

    def specs_of(self, product):
        from attributes import services
        return services.values_for_edit(product)

    def test_bulk_matches_the_product_form(self):
        china, _ = Country.objects.get_or_create(name='China', defaults={'code': 'CN'})
        photo = SimpleUploadedFile('p.jpg', jpeg_bytes(), 'image/jpeg')
        form = {
            'title': 'Form Tote', 'slug': 'form-tote', 'sub_category': self.sub.pk, 'brand': '', 'variant': 'None', 'price': '45',
            'old_price': '0', 'total_quantity': '30', 'weight': '0.3', 'product_type': 'new',
            'available_in_regions': [self.ghana.pk], 'return_period_days': '7', 'unit_cost': '20',
            'fulfilment_type': 'imported', 'sourcing_min_days': '28', 'sourcing_max_days': '42',
            'sourced_from': china.pk, 'life': '', 'image': photo,
            'delivery_options': json.dumps([{'deliveryOptionId': self.express.pk, 'default': True}]),
            'attributes': json.dumps({'material': self.cotton.pk, 'capacity': 12, 'waterproof': False}),
        }
        res = self.client.post('/api/v1/vendor/products/', form, format='multipart')
        self.assertEqual(res.status_code, 201, res.content)
        by_form = Product.objects.get(title='Form Tote')

        products = [{'title': 'Bulk Tote', 'category': self.sub.slug, 'price': '45', 'stock': '30', 'weight_kg': '0.3',
                     'return_days': '7', 'unit_cost': '20', 'stock_type': 'ships from abroad',
                     'sourcing_days': '4-6 weeks', 'sourced_from': 'China', 'delivery_methods': 'Express',
                     'specs': 'Material: cotton; Capacity: 12 L; Waterproof: no'}]
        files = {'photos_0': SimpleUploadedFile('p.jpg', jpeg_bytes(), 'image/jpeg')}
        res = self.client.post(f'{BASE}direct/', {'products': json.dumps(products), **files}, format='multipart')
        self.assertEqual(res.status_code, 201, res.content)
        by_bulk = Product.objects.get(title='Bulk Tote')

        fields = ['status', 'sub_category_id', 'price', 'old_price', 'total_quantity', 'weight', 'volume', 'life',
                  'product_type', 'return_period_days', 'unit_cost', 'fulfilment_type', 'sourcing_min_days',
                  'sourcing_max_days', 'sourced_from_id', 'variant']
        self.assertEqual({f: getattr(by_bulk, f) for f in fields}, {f: getattr(by_form, f) for f in fields})
        self.assertEqual(by_bulk.slug, 'bulk-tote')
        self.assertEqual(list(by_bulk.available_in_regions.all()), list(by_form.available_in_regions.all()))
        self.assertEqual(list(by_bulk.productdeliveryoption_set.values_list('delivery_option', 'default')),
                         list(by_form.productdeliveryoption_set.values_list('delivery_option', 'default')))
        self.assertEqual(self.specs_of(by_bulk), self.specs_of(by_form))
        self.assertEqual(res.data['products'][0]['warnings'], [])

    def test_spec_and_stock_type_mistakes_are_explained(self):
        rows = [
            self.row(title='Bad Spec Name', specs='Colourway: red'),
            self.row(title='Bad Choice', specs='Material: Silk'),
            self.row(title='Bad Yes No', specs='Waterproof: maybe'),
            self.row(title='No Colon', specs='Cotton'),
            self.row(title='Bad Stock Type', stock_type='pre-order'),
            self.row(title='No Days', stock_type='made_to_order'),
            self.row(title='Too Long', stock_type='imported', sourcing_days='20 weeks'),
            self.row(title='Missing Spec Ok'),
        ]
        res = self.upload_csv(rows)
        self.assertEqual(res.status_code, 207, res.content)
        problems = {e['title']: e['errors'] for e in res.data['errors']}
        self.assertIn('Capacity, Material, Waterproof', problems['Bad Spec Name']['specs'])
        self.assertIn('Cotton, Leather', problems['Bad Choice']['specs'])
        self.assertIn('yes or no', problems['Bad Yes No']['specs'])
        self.assertIn('Name: value', problems['No Colon']['specs'])
        self.assertIn('made_to_order', problems['Bad Stock Type']['stock_type'])
        self.assertIn('how many days', problems['No Days']['sourcing_days'])
        self.assertIn('weeks', problems['Too Long']['sourcing_days'])
        saved = res.data['products'][0]
        self.assertEqual(saved['title'], 'Missing Spec Ok')
        self.assertTrue(any('Waterproof' in w for w in saved['warnings']), 'required spec missing: a warning')
        made = Product.objects.get(title='Missing Spec Ok')
        self.assertEqual((made.fulfilment_type, made.sourcing_max_days), ('in_stock', 0))

    def test_version_costs_and_extra_photos(self):
        rows = [
            self.row(title='Mug', unit_cost='10', image_url='https://img.example.com/white.jpg',
                     more_image_urls='https://img.example.com/side.jpg',
                     option1_name='Colour', option1_value='White', seller_sku='MUG-W'),
            self.row(title='Mug', category='', price='50', unit_cost='14', image_url='https://img.example.com/black.jpg',
                     more_image_urls='https://img.example.com/b2.jpg; https://img.example.com/b3.jpg',
                     option1_name='Colour', option1_value='Black', seller_sku='MUG-B'),
        ]
        res = self.upload_csv(rows)
        job = BulkUploadJob.objects.get(pk=res.data['job_id'])
        self.assertEqual(job.success_count, 1, job.errors)
        mug = Product.objects.get(title='Mug')
        self.assertEqual(mug.unit_cost, Decimal('10'))
        self.assertEqual(mug.p_images.count(), 1, 'first row: the product gallery')
        white, black = mug.variants.get(seller_sku='MUG-W'), mug.variants.get(seller_sku='MUG-B')
        self.assertIsNone(white.unit_cost, 'empty = the product cost, like the form')
        self.assertEqual(black.unit_cost, Decimal('14'))
        self.assertEqual(black.variantimage_set.count(), 2)

        self.plan.max_images_per_variant = 0
        self.plan.save()
        rows[0]['title'] = rows[1]['title'] = 'Mug Two'
        rows[0]['seller_sku'], rows[1]['seller_sku'] = 'M2-W', 'M2-B'
        res = self.upload_csv(rows)
        job = BulkUploadJob.objects.get(pk=res.data['job_id'])
        self.assertIn('extra photos per version', job.errors[0]['errors']['more_image_urls'])

    def test_table_version_photos_first_is_main_rest_gallery(self):
        products = [{'rows': [
            {'title': 'Lamp', 'category': self.sub.slug, 'price': '25', 'stock': '3', 'weight_kg': '1',
             'option1_name': 'Colour', 'option1_value': 'Red'},
            {'title': 'Lamp', 'price': '27', 'stock': '2', 'option1_name': 'Colour', 'option1_value': 'Blue'},
        ]}]
        files = {'version_photo_0_1': [SimpleUploadedFile(f'{n}.jpg', jpeg_bytes(), 'image/jpeg') for n in 'abc']}
        res = self.client.post(f'{BASE}direct/', {'products': json.dumps(products), **files}, format='multipart')
        self.assertEqual(res.status_code, 201, res.content)
        blue = Product.objects.get(title='Lamp').variants.get(options__color='Blue')
        self.assertNotEqual(blue.image.name, 'product.jpg')
        self.assertEqual(blue.variantimage_set.count(), 2)

    def test_photo_limit_counts_like_the_form(self):
        # The form allows a main photo plus max_images_per_product more.
        self.plan.max_images_per_product = 2
        self.plan.save()
        ok = [{'title': 'Three Photos', 'category': self.sub.slug, 'price': '5', 'stock': '1', 'weight_kg': '0.1'}]
        files = {'photos_0': [SimpleUploadedFile(f'{n}.jpg', jpeg_bytes(), 'image/jpeg') for n in 'abc']}
        res = self.client.post(f'{BASE}direct/', {'products': json.dumps(ok), **files}, format='multipart')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Product.objects.get(title='Three Photos').p_images.count(), 2)
        ok[0]['title'] = 'Four Photos'
        files = {'photos_0': [SimpleUploadedFile(f'{n}.jpg', jpeg_bytes(), 'image/jpeg') for n in 'abcd']}
        res = self.client.post(f'{BASE}direct/', {'products': json.dumps(ok), **files}, format='multipart')
        self.assertEqual(res.status_code, 400)

    def test_table_sends_specs_like_the_form(self):
        products = [{'title': 'Table Tote', 'category': self.sub.slug, 'price': '5', 'stock': '1', 'weight_kg': '0.1',
                     'specs': {'material': self.cotton.pk, 'capacity': 3, 'waterproof': True}}]
        res = self.client.post(f'{BASE}direct/', {'products': json.dumps(products)}, format='multipart')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.specs_of(Product.objects.get(title='Table Tote')),
                         {'material': self.cotton.pk, 'capacity': Decimal('3'), 'waterproof': True})


class RemoteImageTests(OpsTestCase):

    def test_share_links_become_direct(self):
        self.assertEqual(remote_images.direct_link('https://drive.google.com/file/d/AbC_1-2/view?usp=sharing'),
                         'https://drive.google.com/uc?export=download&id=AbC_1-2')
        self.assertEqual(remote_images.direct_link('https://www.dropbox.com/s/x/a.jpg?dl=0'),
                         'https://www.dropbox.com/s/x/a.jpg?raw=1')

    def test_private_and_odd_addresses_refused(self):
        for url in ('http://127.0.0.1/a.jpg', 'http://169.254.169.254/latest/meta-data', 'ftp://example.com/a.jpg',
                    'http://example.com:8080/a.jpg', 'http://10.0.0.5/a.jpg'):
            with self.subTest(url=url), self.assertRaises(remote_images.RemoteImageError):
                remote_images._check_host(url)

    def test_not_a_photo(self):
        with self.assertRaises(remote_images.RemoteImageError):
            remote_images.to_product_photo(b'<html>login page</html>')
        photo, size = remote_images.to_product_photo(jpeg_bytes(size=2400))
        self.assertEqual(size, (1800, 1800))

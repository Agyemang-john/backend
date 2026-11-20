"""
product/tests_variations.py
Variants that differ by any option (Flavour, Scent, Pack size...), not only size and colour.

Run with:  DB_HOST=localhost python manage.py test product.tests_variations --keepdb
"""

import json
from decimal import Decimal

from django.core.cache import cache
from rest_framework.test import APIClient

from address.models import Country
from product import variations
from product.models import Color, Product, Size, Variants
from vendor.tests_operations import OpsTestCase, vendor_client

PRODUCTS = '/api/v1/vendor/products/'


class VariationTestCase(OpsTestCase):

    def setUp(self):
        super().setUp()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        self.seller = vendor_client(self.owner)
        self.ghana, _ = Country.objects.get_or_create(name='Ghana', defaults={'code': 'GH'})

    def form(self, axes, variants, **extra):
        data = {
            'title': 'Protein Shake', 'slug': 'protein-shake', 'available_in_regions': [self.ghana.pk],
            'price': '120.00', 'total_quantity': '0', 'sub_category': self.sub.pk, 'brand': '',
            'variant': 'None', 'product_type': 'grocery',
            'variation_axes': json.dumps(axes), 'variants': json.dumps(variants),
        }
        data.update(extra)
        return data

    @staticmethod
    def row(options, price='120.00', quantity=5, **extra):
        return {'id': None, 'options': options, 'price': price, 'quantity': quantity, **extra}

    def create(self, axes, variants, **extra):
        return self.seller.post(PRODUCTS, self.form(axes, variants, **extra), format='multipart')


class SellerSavesOptionsTests(VariationTestCase):

    def test_flavour_and_size_product(self):
        res = self.create(
            [{'key': 'flavor'}, {'key': 'size'}],
            [self.row({'flavor': ' Vanilla ', 'size': '500 g'}),
             self.row({'flavor': 'Chocolate', 'size': '500 g'}, price='125.00'),
             self.row({'flavor': 'Vanilla', 'size': '1 kg'}, price='200.00', quantity=0)])
        self.assertEqual(res.status_code, 201, res.content)
        product = Product.objects.get(pk=res.data['id'])
        self.assertEqual(product.variant, 'Options')
        self.assertEqual(product.variation_axes, [{'key': 'flavor', 'label': 'Flavour'}, {'key': 'size', 'label': 'Size'}])

        vanilla = product.variants.get(price=Decimal('120.00'))
        self.assertEqual(vanilla.options, {'flavor': 'Vanilla', 'size': '500 g'})
        self.assertEqual(vanilla.title, 'Protein Shake - Vanilla - 500 g')
        self.assertEqual(vanilla.option_label, 'Vanilla / 500 g')
        self.assertEqual(vanilla.size.name, '500 g', 'size mirrored for the size filter and size charts')
        self.assertIsNone(vanilla.color)
        self.assertEqual(product.get_stock_quantity(), 10)

        # The edit page reads the options back.
        res = self.seller.get(f'{PRODUCTS}{product.pk}/')
        self.assertEqual([a['key'] for a in res.data['variation_axes']], ['flavor', 'size'])
        self.assertIn({'flavor': 'Chocolate', 'size': '500 g'}, [v['options'] for v in res.data['variants']])

    def test_colour_and_size_keep_the_old_type(self):
        Color.objects.create(name='Navy', code='#001f3f')
        res = self.create([{'key': 'color'}, {'key': 'size'}],
                          [self.row({'color': 'navy', 'size': 'M'}), self.row({'color': 'Navy', 'size': 'L'})])
        self.assertEqual(res.status_code, 201, res.content)
        product = Product.objects.get(pk=res.data['id'])
        self.assertEqual(product.variant, 'Size-Color', 'older clients still understand it')
        self.assertEqual(Color.objects.filter(name__iexact='navy').count(), 1, 'existing colour reused')
        self.assertTrue(all(v.color.code == '#001f3f' for v in product.variants.all()))

    def test_custom_option(self):
        res = self.create([{'key': 'custom', 'label': 'Roast level'}],
                          [self.row({'x_roast_level': 'Dark'}), self.row({'x_roast_level': 'Medium'})])
        self.assertEqual(res.status_code, 201, res.content)
        product = Product.objects.get(pk=res.data['id'])
        self.assertEqual(product.variation_axes, [{'key': 'x_roast_level', 'label': 'Roast level'}])
        self.assertEqual(product.variant, 'Options')

    def test_rejections(self):
        cases = [
            ([{'key': 'flavor'}], [self.row({'flavor': 'Mint'}), self.row({'flavor': 'mint'})], 'variants', 'same'),
            ([{'key': 'flavor'}, {'key': 'size'}], [self.row({'flavor': 'Mint'})], 'variants', 'size'),
            ([{'key': k} for k in ('color', 'size', 'flavor', 'scent')], [], 'variation_axes', 'up to 3'),
            ([{'key': 'flavor'}, {'key': 'flavor'}], [], 'variation_axes', 'twice'),
            ([{'key': 'custom', 'label': ''}], [], 'variation_axes', 'name'),
        ]
        before = Product.objects.count()
        for axes, rows, field, words in cases:
            with self.subTest(axes=axes):
                res = self.create(axes, rows)
                self.assertEqual(res.status_code, 400, res.content)
                self.assertIn(words, str(res.data[field][0]))
        self.assertEqual(Product.objects.count(), before)

    def test_editing_changes_options_and_removes_versions(self):
        res = self.create([{'key': 'scent'}], [self.row({'scent': 'Lavender'}), self.row({'scent': 'Citrus'})])
        product = Product.objects.get(pk=res.data['id'])
        lavender = product.variants.get(options__scent='Lavender')
        res = self.seller.put(f'{PRODUCTS}{product.pk}/', self.form(
            [{'key': 'scent'}, {'key': 'pack_size'}],
            [self.row({'scent': 'Lavender', 'pack_size': 'Pack of 3'}, id=lavender.pk)]), format='multipart')
        self.assertEqual(res.status_code, 200, res.content)
        product.refresh_from_db()
        self.assertEqual(product.variants.count(), 1)
        lavender.refresh_from_db()
        self.assertEqual(lavender.options, {'scent': 'Lavender', 'pack_size': 'Pack of 3'})

    def test_older_client_without_options_still_works(self):
        size = Size.objects.create(name='XL')
        res = self.seller.post(PRODUCTS, {
            'title': 'Old Tee', 'slug': 'old-tee', 'available_in_regions': [self.ghana.pk], 'price': '50.00',
            'total_quantity': '0', 'sub_category': self.sub.pk, 'brand': '', 'variant': 'Size', 'product_type': 'new',
            'variants': json.dumps([{'id': None, 'size': size.pk, 'price': '50.00', 'quantity': 2}]),
        }, format='multipart')
        self.assertEqual(res.status_code, 201, res.content)
        variant = Variants.objects.get(product_id=res.data['id'])
        self.assertEqual(variant.options, {'size': 'XL'})
        self.assertEqual([a['key'] for a in res.data['variation_axes']], ['size'])


class StorefrontSelectorTests(VariationTestCase):

    def setUp(self):
        super().setUp()
        res = self.create(
            [{'key': 'flavor'}, {'key': 'size'}],
            [self.row({'flavor': 'Vanilla', 'size': '500 g'}),
             self.row({'flavor': 'Chocolate', 'size': '500 g'}, quantity=0),
             self.row({'flavor': 'Vanilla', 'size': '1 kg'}),
             self.row({'flavor': 'Strawberry', 'size': '1 kg'})])
        self.product = Product.objects.get(pk=res.data['id'])
        Product.objects.filter(pk=self.product.pk).update(status='published')
        self.product.refresh_from_db()
        self.v = {(v.options['flavor'], v.options['size']): v for v in self.product.variants.all()}

    def pick(self, flavor, size):
        return self.v[(flavor, size)]

    def test_selector_matrix(self):
        current = self.pick('Vanilla', '500 g')
        rows = variations.selector(self.product, current, list(self.product.variants.order_by('id')))
        flavours = {e['value']: e for e in rows[0]['values']}
        sizes = {e['value']: e for e in rows[1]['values']}
        self.assertEqual(rows[0]['selected'], 'Vanilla')

        # Chocolate exists in 500 g but is sold out.
        self.assertEqual(flavours['Chocolate']['variant_id'], self.pick('Chocolate', '500 g').pk)
        self.assertTrue(flavours['Chocolate']['available'])
        self.assertFalse(flavours['Chocolate']['in_stock'])
        # Strawberry only comes in 1 kg: clicking it opens that one.
        self.assertFalse(flavours['Strawberry']['available'])
        self.assertEqual(flavours['Strawberry']['variant_id'], self.pick('Strawberry', '1 kg').pk)
        # 1 kg keeps Vanilla.
        self.assertEqual(sizes['1 kg']['variant_id'], self.pick('Vanilla', '1 kg').pk)
        self.assertTrue(sizes['500 g']['selected'])

    def test_product_page_api(self):
        url = f'/api/v1/product/{self.product.sku}/{self.product.slug}/'
        res = APIClient().get(url)
        self.assertEqual(res.status_code, 200, res.content)
        data = res.data['variant_data']
        self.assertEqual([a['key'] for a in data['axes']], ['flavor', 'size'])
        self.assertEqual(data['variant']['option_label'], 'Vanilla / 500 g', 'opens on the first in-stock version')
        self.assertEqual(len(data['options'][0]['values']), 3)

        res = APIClient().get(url, {'variantid': self.pick('Strawberry', '1 kg').pk})
        self.assertEqual(res.data['variant_data']['options'][0]['selected'], 'Strawberry')
        self.assertEqual(res.data['variant_data']['options'][1]['selected'], '1 kg')

    def test_legacy_size_colour_product_reads_the_same_way(self):
        red, _ = Color.objects.get_or_create(name='Legacy Red', defaults={'code': '#ff0000'})
        small = Size.objects.create(name='LS')
        tee = Product.objects.create(title='Legacy Tee', sub_category=self.sub, vendor=self.vendor,
                                     status='published', price=Decimal('40'), variant='Size-Color')
        v = Variants.objects.create(product=tee, size=small, color=red, quantity=1, price=Decimal('40'))
        self.assertEqual(v.options, {'size': 'LS', 'color': 'Legacy Red'})
        self.assertEqual([a['key'] for a in variations.effective_axes(tee)], ['size', 'color'])
        rows = variations.selector(tee, v, [v])
        self.assertEqual(rows[1]['values'][0]['swatch'], '#ff0000')

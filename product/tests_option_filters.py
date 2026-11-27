"""
product/tests_option_filters.py
Listing filters for variant options (Flavour, Pack size, seller-named options)
on category, brand, search and seller pages.

Run with:  DB_HOST=localhost python manage.py test product.tests_option_filters --keepdb
"""
from decimal import Decimal

from django.core.cache import cache
from django.http import QueryDict
from django.test import SimpleTestCase
from rest_framework.test import APIClient

from product import option_filters
from product.models import Brand, Product, Variants
from vendor.tests_operations import OpsTestCase


class ParseTests(SimpleTestCase):

    def test_reads_options_any_capitals_and_skips_colour_and_size(self):
        params = QueryDict('o_flavor=Vanilla,%20CHOCOLATE&o_x_fabric=silk&o_x_fabric=Wool&o_color=red&o_BAD-KEY=x&page=2')
        self.assertEqual(option_filters.parse(params),
                         {'flavor': ['vanilla', 'chocolate'], 'x_fabric': ['silk', 'wool']})

    def test_normalise(self):
        self.assertEqual(option_filters.normalise('  Pack  of 6, Large '), 'pack of 6 large')


class OptionFilterTests(OpsTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1}, None)
        self.client = APIClient()
        self.brand = Brand.objects.create(title='Shake Co')
        Product.objects.filter(pk=self.product.pk).update(status='draft')   # keep the shared fixture out
        self.shake = self.make('Whey Shake', [{'key': 'flavor', 'label': 'Flavour'}, {'key': 'pack_size', 'label': 'Pack size'}],
                               [{'flavor': 'Vanilla', 'pack_size': 'Pack of 6'}, {'flavor': 'Chocolate', 'pack_size': 'Single'}])
        self.bar = self.make('Protein Bar', [{'key': 'flavor', 'label': 'Flavour'}],
                             [{'flavor': 'vanilla '}, {'flavor': 'Peanut'}])
        # Two stores name their own option the same way: one filter.
        self.scarf = self.make('Shea Scarf', [{'key': 'x_fabric', 'label': 'Fabric'}], [{'x_fabric': 'Silk'}, {'x_fabric': 'Wool'}])
        self.wrap = self.make('Shea Wrap', [{'key': 'x_fabric', 'label': 'Fabric'}], [{'x_fabric': 'silk'}])

    def make(self, title, axes, options):
        product = Product.objects.create(
            title=title, sub_category=self.sub, vendor=self.vendor, brand=self.brand, status='published',
            variant='Options', variation_axes=axes, price=Decimal('50'), total_quantity=10,
            description=f'<p>{title} protein</p>')
        for opts in options:
            Variants.objects.create(product=product, options=opts, price=Decimal('50'), quantity=3)
        return product

    def titles(self, res, key='products_with_details'):
        rows = res.data[key]
        return sorted((r.get('product') or r).get('title') for r in rows)

    def groups(self, res):
        return {g['param']: g for g in res.data['attributes'] if g.get('kind') == 'option'}

    def test_category_page_filters_and_counts(self):
        url = f'/api/v1/product/category/{self.sub.slug}/'
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200, res.data)
        groups = self.groups(res)
        self.assertEqual(groups['o_flavor']['name'], 'Flavour')
        flavours = {o['value']: o['count'] for o in groups['o_flavor']['options']}
        self.assertEqual(flavours, {'Vanilla': 2, 'Chocolate': 1, 'Peanut': 1}, 'any capitals and spacing = one value')
        self.assertEqual(groups['o_x_fabric']['name'], 'Fabric')
        self.assertEqual({o['value']: o['count'] for o in groups['o_x_fabric']['options']}, {'Silk': 2, 'Wool': 1})
        self.assertNotIn('o_pack_size', groups, 'an option only one product has is not worth a filter')

        res = self.client.get(url, {'o_flavor': 'vanilla'})
        self.assertEqual(self.titles(res), ['Protein Bar', 'Whey Shake'])
        flavours = {o['slug']: o['count'] for o in self.groups(res)['o_flavor']['options']}
        self.assertEqual(flavours['chocolate'], 1, 'ticking Vanilla keeps the other flavours')

        res = self.client.get(url, {'o_flavor': 'vanilla,peanut', 'o_x_fabric': 'silk'})
        self.assertEqual(self.titles(res), [], 'different options all have to match')
        res = self.client.get(url, {'o_x_fabric': 'SILK'})
        self.assertEqual(self.titles(res), ['Shea Scarf', 'Shea Wrap'])

    def test_brand_search_and_seller_pages(self):
        res = self.client.get(f'/api/v1/product/brand/{self.brand.slug}/', {'o_flavor': 'chocolate'})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.titles(res), ['Whey Shake'])
        self.assertIn('o_flavor', self.groups(res))

        res = self.client.get('/api/v1/product/search/', {'q': 'protein', 'o_flavor': 'peanut'})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.titles(res), ['Protein Bar'])

        res = self.client.get(f'/api/v1/vendor/seller-detail/{self.vendor.slug}/products/', {'o_x_fabric': 'wool'})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.titles(res, 'results'), ['Shea Scarf'])
        self.assertIn('o_x_fabric', self.groups(res))

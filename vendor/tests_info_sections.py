"""
vendor/tests_info_sections.py
Product information sections the seller names ("Processor", "Ingredients"...),
from the product form and from bulk upload, and the product page's copy.

Run with:  DB_HOST=localhost python manage.py test vendor.tests_info_sections --keepdb
"""
import json

from django.test import SimpleTestCase

from product import info_sections
from product.models import Product
from vendor.tests_bulk_upload import BulkTestCase


class CleanSectionsTests(SimpleTestCase):

    def test_cleans_titles_and_bodies_and_drops_empty_cards(self):
        sections = info_sections.clean_sections(json.dumps([
            {'title': '  Processor ', 'body': '<p>Intel Core i5</p><script>alert(1)</script>'},
            {'title': '', 'body': '<p></p>'},
            {'title': 'Display', 'body': '<p>14" FHD</p>'},
        ]))
        self.assertEqual(sections, [{'title': 'Processor', 'body': '<p>Intel Core i5</p>'},
                                    {'title': 'Display', 'body': '<p>14" FHD</p>'}])

    def test_mistakes_get_plain_messages(self):
        cases = [
            ([{'title': '', 'body': '<p>Text</p>'}], 'needs a title'),
            ([{'title': 'Storage', 'body': '<p> </p>'}], 'is empty'),
            ([{'title': 'Care', 'body': '<p>a</p>'}, {'title': 'care', 'body': '<p>b</p>'}], 'two sections'),
            ([{'title': 'x' * 61, 'body': '<p>a</p>'}], 'too long'),
            ([{'title': f'S{i}', 'body': '<p>a</p>'} for i in range(13)], 'at most 12'),
            ('not json', 'could not be read'),
        ]
        for raw, message in cases:
            with self.subTest(message):
                with self.assertRaisesMessage(info_sections.SectionError, message):
                    info_sections.clean_sections(raw if isinstance(raw, str) else json.dumps(raw))

    def test_older_products_show_their_free_text_as_one_section(self):
        old = Product(specifications='<p>100% cotton</p>')
        self.assertEqual(info_sections.sections_for(old), [{'title': 'Additional details', 'body': '<p>100% cotton</p>'}])
        self.assertEqual(info_sections.sections_for(Product()), [], "the model's old 'Black' default is not shown")


class ProductFormSectionsTests(BulkTestCase):

    def patch(self, **data):
        return self.client.patch(f'/api/v1/vendor/products/{self.product.id}/', data, format='multipart')

    def test_seller_names_their_own_sections(self):
        sections = [{'title': 'Ingredients', 'body': '<p>Water, sugar</p>'},
                    {'title': 'Nutrition facts', 'body': '<table><tr><td>Energy</td><td>120 kcal</td></tr></table>'}]
        res = self.patch(info_sections=json.dumps(sections))
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual([s['title'] for s in self.product.info_sections], ['Ingredients', 'Nutrition facts'])
        self.assertIn('<h3>Ingredients</h3><p>Water, sugar</p>', self.product.specifications, 'kept for search')
        self.assertEqual([s['title'] for s in res.data['info_sections']], ['Ingredients', 'Nutrition facts'])

        # Removing them all really removes them.
        res = self.patch(info_sections='[]')
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual((self.product.info_sections, self.product.specifications), ([], ''))

    def test_bad_sections_are_refused_and_nothing_changes(self):
        res = self.patch(info_sections=json.dumps([{'title': '', 'body': '<p>Text</p>'}]))
        self.assertEqual(res.status_code, 400)
        self.assertIn('needs a title', str(res.data['info_sections']))

    def test_not_sending_sections_keeps_them(self):
        self.patch(info_sections=json.dumps([{'title': 'Display', 'body': '<p>OLED</p>'}]))
        res = self.patch(price='99')
        self.assertEqual(res.status_code, 200, res.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.info_sections, [{'title': 'Display', 'body': '<p>OLED</p>'}])

    def test_editing_an_older_product_shows_its_details_as_a_section(self):
        Product.objects.filter(pk=self.product.pk).update(specifications='<p>Hand wash only</p>')
        res = self.client.get(f'/api/v1/vendor/products/{self.product.id}/')
        self.assertEqual(res.data['info_sections'], [{'title': 'Additional details', 'body': '<p>Hand wash only</p>'}])


class BulkSectionsTests(BulkTestCase):

    def test_sections_column_and_old_specifications_column(self):
        self.upload_csv([self.row(sections='Material: 100% cotton | Care: Machine wash cold',
                                  specifications='Made in Ghana.')])
        product = Product.objects.get(vendor=self.vendor, title='Cotton Tote')
        self.assertEqual(product.info_sections, [
            {'title': 'Material', 'body': '<p>100% cotton</p>'},
            {'title': 'Care', 'body': '<p>Machine wash cold</p>'},
            {'title': 'Additional details', 'body': '<p>Made in Ghana.</p>'},
        ])

    def test_badly_written_sections_are_a_row_error(self):
        res = self.upload_csv([self.row(sections='Just some text without a title')])
        self.assertFalse(Product.objects.filter(vendor=self.vendor, title='Cotton Tote').exists())
        self.assertIn('Title: text', json.dumps(res.data))

    def test_browser_table_sends_sections_like_the_form(self):
        products = [{'title': 'Shea Butter', 'category': self.sub.slug, 'price': '30', 'stock': '5', 'weight_kg': '0.4',
                     'sections': [{'title': 'Ingredients', 'body': 'Raw shea butter'},
                                  {'title': 'How to use', 'body': 'Warm in hands.\n\nApply to skin.'}]}]
        res = self.client.post('/api/v1/vendor/products/bulk-upload/direct/', {'products': json.dumps(products)}, format='multipart')
        self.assertIn(res.status_code, (200, 201), res.content)
        product = Product.objects.get(vendor=self.vendor, title='Shea Butter')
        self.assertEqual(product.info_sections, [
            {'title': 'Ingredients', 'body': '<p>Raw shea butter</p>'},
            {'title': 'How to use', 'body': '<p>Warm in hands.</p><p>Apply to skin.</p>'},
        ])

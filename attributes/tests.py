"""
Structured attributes: seller values, listing filters + counts, specs, compare, search.

Run with:  DB_HOST=localhost python manage.py test attributes.tests --keepdb
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from attributes.models import AttributeDefinition, AttributeOption, ProductAttributeValue
from attributes.services import save_values
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


def make_user(n):
    user = User.objects.create_user(f'Attr{n}', 'Test', f'attr{n}@example.com', f'+23324500{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class AttributeTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owner = make_user(1)
        self.vendor = Vendor.objects.create(name='Attr Shop', user=self.owner, email='attr-shop@example.com',
                                            contact='+233550007777', is_approved=True, status='VERIFIED',
                                            is_subscribed=True)
        main = Main_Category.objects.create(title='Attr Electronics')
        self.cat = Category.objects.create(title='Attr Phones', main_category=main)
        self.sub = Sub_Category.objects.create(title='Attr Smartphones', category=self.cat)
        self.brand = Brand.objects.create(title='Attr Brand')

        self.ram = AttributeDefinition.objects.create(name='RAM', slug='ram', type='choice', unit='GB')
        self.ram.categories.add(self.cat)                     # via parent category
        self.r8 = AttributeOption.objects.create(definition=self.ram, value='8 GB')
        self.r16 = AttributeOption.objects.create(definition=self.ram, value='16 GB')
        self.screen = AttributeDefinition.objects.create(name='Screen', slug='screen', type='number', unit='in')
        self.screen.sub_categories.add(self.sub)
        self.nfc = AttributeDefinition.objects.create(name='NFC', slug='nfc', type='boolean', required=True)
        self.nfc.sub_categories.add(self.sub)
        self.color = AttributeDefinition.objects.create(name='Finish', slug='finish', type='choice', multi=True)
        self.color.sub_categories.add(self.sub)
        self.matte = AttributeOption.objects.create(definition=self.color, value='Matte')
        self.gloss = AttributeOption.objects.create(definition=self.color, value='Gloss')

        self.phones = []
        for i, (ram, screen, nfc) in enumerate([(self.r8, '6.1', True), (self.r8, '6.7', False), (self.r16, '6.7', True)]):
            p = Product.objects.create(title=f'Attr Phone {i}', sub_category=self.sub, vendor=self.vendor,
                                       status='published', price=Decimal(100 + i), total_quantity=3, brand=self.brand)
            save_values(p, {'ram': ram.pk, 'screen': screen, 'nfc': nfc})
            self.phones.append(p)

    def listing(self, **params):
        return APIClient().get(f'/api/v1/product/category/{self.sub.slug}/', params)

    def test_filters_and_counts(self):
        res = self.listing(a_ram='8-gb')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['total'], 2)
        groups = {g['slug']: g for g in res.data['attributes']}
        ram = {o['slug']: o for o in groups['ram']['options']}
        # The selected attribute still shows the other option's count.
        self.assertEqual(ram['8-gb']['count'], 2)
        self.assertTrue(ram['8-gb']['selected'])
        self.assertEqual(ram['16-gb']['count'], 1)
        self.assertEqual(groups['nfc']['count'], 1, "NFC count within the 8 GB phones")
        self.assertEqual(groups['screen']['min'], Decimal('6.1'))

        self.assertEqual(self.listing(a_ram='8-gb,16-gb', a_nfc='1').data['total'], 2)
        self.assertEqual(self.listing(a_screen_min='6.5').data['total'], 2)
        self.assertEqual(self.listing(a_ram='nonsense').data['total'], 3, "unknown option ignored")

    def test_specs_on_product_detail(self):
        p = self.phones[0]
        res = APIClient().get(f'/api/v1/product/{p.sku}/{p.slug}/')
        specs = {s['slug']: s['value'] for s in res.data['specs']}
        self.assertEqual(specs, {'ram': '8 GB', 'screen': '6.1 in', 'nfc': 'Yes'})

    def test_attribute_words_are_searchable(self):
        save_values(self.phones[2], {'finish': [self.matte.pk, self.gloss.pk]})
        res = APIClient().get('/api/v1/product/search/', {'q': 'matte'})
        titles = [row['product']['title'] for row in res.data['products_with_details']]
        self.assertEqual(titles, ['Attr Phone 2'])

    def test_seller_edits_values(self):
        seller = APIClient()
        seller.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owner).access_token)
        seller.credentials(HTTP_X_USER_TYPE='vendor')
        url = f'/api/v1/attributes/seller/products/{self.phones[1].pk}/'
        res = seller.get(url)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([d['slug'] for d in res.data['definitions']], ['finish', 'nfc', 'ram', 'screen'])
        self.assertEqual(res.data['values']['ram'], self.r8.pk)

        res = seller.put(url, {'values': {'ram': self.r16.pk, 'finish': [self.matte.pk]}}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['values']['ram'], self.r16.pk)
        self.assertEqual(res.data['values']['screen'], Decimal('6.7000'), "untouched attributes kept")

        bad = seller.put(url, {'values': {'ram': [self.r8.pk, self.r16.pk]}}, format='json')
        self.assertEqual(bad.status_code, 400)
        bad = seller.put(url, {'values': {'ram': self.matte.pk}}, format='json')
        self.assertEqual(bad.status_code, 400, "option from another attribute")
        bad = seller.put(url, {'values': {'battery': 5000}}, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_required_missing_reported(self):
        p = Product.objects.create(title='Attr Phone X', sub_category=self.sub, vendor=self.vendor,
                                   status='published', price=Decimal('1'), brand=self.brand)
        self.assertEqual(save_values(p, {'ram': self.r8.pk}), ['nfc'])

    def test_compare(self):
        ids = ','.join(str(p.pk) for p in self.phones[:2])
        res = APIClient().get('/api/v1/attributes/compare/', {'ids': ids})
        self.assertEqual([c['title'] for c in res.data['products']], ['Attr Phone 0', 'Attr Phone 1'])
        rows = {r['slug']: r for r in res.data['rows']}
        self.assertEqual(rows['screen']['values'], ['6.1 in', '6.7 in'])
        self.assertTrue(rows['screen']['differs'])
        self.assertFalse(rows['ram']['differs'])

    def test_listing_query_count_does_not_grow_with_products(self):
        def count():
            from django.core.cache import cache
            from django.db import connection
            from django.test.utils import CaptureQueriesContext
            cache.clear()
            cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
            with CaptureQueriesContext(connection) as ctx:
                self.listing(a_ram='8-gb')
            return len(ctx.captured_queries)
        before = count()
        for i in range(10):
            p = Product.objects.create(title=f'Attr Bulk {i}', sub_category=self.sub, vendor=self.vendor,
                                       status='published', price=Decimal('5'), brand=self.brand)
            ProductAttributeValue.objects.create(product=p, definition=self.ram, option=self.r8)
        self.assertEqual(count(), before)

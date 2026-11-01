"""
Multi-seller catalog: GTIN grouping, featured offer, listing dedupe, "sell one like this".

Run with:  DB_HOST=localhost python manage.py test catalog.tests --keepdb
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.buybox import recompute
from catalog.services import clean_gtin, group
from ecommerce.celery import app as celery_app
from product.models import Brand, CatalogItem, Category, Main_Category, Product, ProductImages, Sub_Category
from userauths.models import User
from userauths.tokens import CustomVendorRefreshToken
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}
GTIN = '5901234123457'   # valid EAN-13


def make_user(n):
    user = User.objects.create_user(f'Cat{n}', 'Test', f'cat{n}@example.com', f'+23324700{n:04d}', 'Str0ng!Passw0rd')
    User.objects.filter(pk=user.pk).update(is_active=True, email_verified_at=timezone.now())
    return User.objects.get(pk=user.pk)


@override_settings(**TEST_SETTINGS)
class CatalogTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.owners = [make_user(i) for i in range(1, 4)]
        self.shops = [Vendor.objects.create(name=f'Cat Shop {i}', user=u, email=f'cat-shop{i}@example.com',
                                            contact=f'+23355000900{i}', is_approved=True, status='VERIFIED',
                                            handling_days=1)
                      for i, u in enumerate(self.owners)]
        main = Main_Category.objects.create(title='Cat Main')
        self.sub = Sub_Category.objects.create(title='Cat Sub', category=Category.objects.create(title='Cat Cat', main_category=main))
        self.brand = Brand.objects.create(title='Cat Brand')

    def listing(self, shop, price, gtin=GTIN, title='Cat Blender 500W', qty=5):
        with self.captureOnCommitCallbacks(execute=True):
            return Product.objects.create(title=title, sub_category=self.sub, vendor=shop, status='published',
                                          price=Decimal(price), total_quantity=qty, brand=self.brand, gtin=gtin)

    def test_gtin_check_digit(self):
        self.assertEqual(clean_gtin('590-1234-123457'), '0' + GTIN)
        self.assertIsNone(clean_gtin('5901234123458'))
        self.assertIsNone(clean_gtin('123'))

    def test_same_title_allowed_across_stores_not_within(self):
        self.listing(self.shops[0], '100', gtin='')
        self.listing(self.shops[1], '100', gtin='')
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.create(title='Cat Blender 500W', sub_category=self.sub, vendor=self.shops[0],
                                   price=Decimal('1'), brand=self.brand)

    def test_same_gtin_groups_and_cheapest_in_stock_wins(self):
        a = self.listing(self.shops[0], '120')
        b = self.listing(self.shops[1], '100')
        c = self.listing(self.shops[2], '90', qty=0)          # cheapest, but out of stock
        for p in (a, b, c):
            p.refresh_from_db()
        self.assertEqual(len({a.catalog_item_id, b.catalog_item_id, c.catalog_item_id}), 1)
        self.assertEqual([a.is_buybox_winner, b.is_buybox_winner, c.is_buybox_winner], [False, True, False])
        item = CatalogItem.objects.get(pk=a.catalog_item_id)
        self.assertEqual((item.offer_count, item.lowest_price, item.winner_id), (3, Decimal('100.00'), b.pk))

        # Restocked by an F() update (no signal): the periodic recompute picks it up.
        from django.db.models import F
        Product.objects.filter(pk=c.pk).update(total_quantity=F('total_quantity') + 3)
        recompute([item.pk])
        c.refresh_from_db()
        self.assertTrue(c.is_buybox_winner)

    def test_slow_dispatch_can_lose_to_slightly_dearer_offer(self):
        Vendor.objects.filter(pk=self.shops[0].pk).update(handling_days=7)   # +9%
        a = self.listing(self.shops[0], '100')
        b = self.listing(self.shops[1], '105')
        a.refresh_from_db(); b.refresh_from_db()
        self.assertTrue(b.is_buybox_winner)
        self.assertFalse(a.is_buybox_winner)

    def test_paused_store_hands_over(self):
        a = self.listing(self.shops[0], '90')
        b = self.listing(self.shops[1], '100')
        a.refresh_from_db()
        self.assertTrue(a.is_buybox_winner)
        with self.captureOnCommitCallbacks(execute=True):
            shop = Vendor.objects.get(pk=self.shops[0].pk)
            shop.shop_paused = True
            shop.save()
        b.refresh_from_db()
        self.assertTrue(b.is_buybox_winner)

    def test_listing_shows_one_card_and_offers_endpoint(self):
        a = self.listing(self.shops[0], '120')
        b = self.listing(self.shops[1], '100')
        res = APIClient().get(f'/api/v1/product/category/{self.sub.slug}/')
        self.assertEqual(res.data['total'], 1)
        self.assertEqual(res.data['products_with_details'][0]['product']['id'], b.pk)

        offers = APIClient().get(f'/api/v1/catalog/products/{a.pk}/offers/').data
        self.assertEqual(offers['offer_count'], 2)
        self.assertEqual(offers['offers'][0]['product_id'], b.pk)
        self.assertTrue(offers['offers'][0]['is_featured'])

    def test_staff_grouping_rules(self):
        a = self.listing(self.shops[0], '120', gtin='', title='Blender A')
        b = self.listing(self.shops[1], '100', gtin='', title='Blender B')
        with self.captureOnCommitCallbacks(execute=True):
            item = group([a, b])
        self.assertEqual(Product.objects.filter(catalog_item=item).count(), 2)
        a2 = self.listing(self.shops[0], '1', gtin='', title='Blender A2')
        from catalog.services import CatalogError
        with self.assertRaises(CatalogError):
            group([a, a2])   # same store twice

    def test_seller_offers_existing_item(self):
        source = self.listing(self.shops[0], '120', gtin='')
        ProductImages.objects.create(product=source, images='product_images/x.jpg')
        c = APIClient()
        c.cookies['vendor_access'] = str(CustomVendorRefreshToken.for_user(self.owners[1]).access_token)
        c.credentials(HTTP_X_USER_TYPE='vendor')
        self.assertTrue(c.get('/api/v1/catalog/seller/offers/', {'product': source.pk}).data['can_offer'])
        with self.captureOnCommitCallbacks(execute=True):
            res = c.post('/api/v1/catalog/seller/offers/', {'product': source.pk, 'price': '99.00', 'quantity': 4},
                         format='json')
        self.assertEqual(res.status_code, 201, res.content)
        offer = Product.objects.get(pk=res.data['id'])
        source.refresh_from_db()
        self.assertEqual(offer.vendor, self.shops[1])
        self.assertEqual(offer.catalog_item_id, source.catalog_item_id)
        self.assertEqual(offer.title, source.title)
        self.assertEqual(offer.p_images.count(), 1)
        self.assertTrue(offer.is_buybox_winner)
        self.assertFalse(source.is_buybox_winner)
        again = c.post('/api/v1/catalog/seller/offers/', {'product': source.pk, 'price': '98', 'quantity': 1}, format='json')
        self.assertEqual(again.status_code, 409)

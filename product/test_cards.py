"""
Storefront card details (product/cards.py): swatches, size range, delivery days,
badges, caching, and that a listing page costs a bounded number of queries.

Run with:  DB_HOST=localhost python manage.py test product.test_cards --keepdb
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from product import cards
from product.models import (Brand, Category, Color, DeliveryOption, FlashSale, Main_Category, Product,
                            ProductDeliveryOption, Size, Sub_Category, Variants)
from userauths.models import User
from vendor.models import Vendor

celery_app.conf.task_always_eager = True


@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
)
class CardTests(TestCase):
    def setUp(self):
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        owner = User.objects.create_user('Card', 'Owner', 'card-owner@example.com', '+233249100001', 'Str0ng!Passw0rd')
        self.shop = Vendor.objects.create(name='Card Shop', user=owner, email='card-shop@example.com',
                                          contact='+233550007100', is_approved=True, status='VERIFIED')
        main = Main_Category.objects.create(title='Card Main')
        self.sub = Sub_Category.objects.create(title='Card Tees',
                                               category=Category.objects.create(title='Card Cat', main_category=main))
        self.brand = Brand.objects.create(title='Card Brand')

    def product(self, title, **kw):
        defaults = dict(sub_category=self.sub, vendor=self.shop, brand=self.brand, status='published',
                        price=Decimal('100'), total_quantity=50)
        defaults.update(kw)
        return Product.objects.create(title=title, **defaults)

    def test_swatches_sizes_delivery_and_flash_badge(self):
        tee = self.product('Card Tee', variant='Size-Color')
        Product.objects.filter(pk=tee.pk).update(date=timezone.now() - timedelta(days=60))
        navy, white = Color.objects.create(name='Navy', code='#001F5B'), Color.objects.create(name='White', code='#FFF')
        for size in ('3XL', 'XS', 'M'):
            s = Size.objects.create(name=size)
            for c in (navy, white):
                Variants.objects.create(product=tee, size=s, color=c, price=Decimal('100'), quantity=3)
        fast = DeliveryOption.objects.create(name='Express', description='', min_days=1, max_days=2)
        slow = DeliveryOption.objects.create(name='Standard', description='', min_days=3, max_days=6)
        for opt in (slow, fast):
            ProductDeliveryOption.objects.create(product=tee, delivery_option=opt)
        now = timezone.now()
        FlashSale.objects.create(product=tee, sale_price=Decimal('80'), original_price=Decimal('100'), label='clearance',
                                 start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=5))

        card = cards.card_extras([tee])[tee.pk]
        self.assertEqual([c['name'] for c in card['colors']], ['Navy', 'White'])
        self.assertEqual(card['color_count'], 2)
        self.assertEqual(card['size_label'], 'Sizes XS-3XL')
        self.assertEqual(card['options'], 6)
        self.assertEqual(card['delivery'], {'min_days': 1, 'max_days': 2}, "fastest option wins")
        self.assertEqual((card['badge']['kind'], card['badge']['label']), ('flash', 'Clearance'))

    def test_simple_product_badges(self):
        low = self.product('Card Mug', total_quantity=3)
        Product.objects.filter(pk=low.pk).update(date=timezone.now() - timedelta(days=60))
        new = self.product('Card Cap')
        got = cards.card_extras([low.pk, new.pk])
        self.assertEqual(got[low.pk]['badge']['label'], 'Only 3 left')
        self.assertEqual(got[new.pk]['badge']['kind'], 'new')
        self.assertEqual((got[low.pk]['options'], got[low.pk]['size_label'], got[low.pk]['delivery']), (0, '', None))

    def test_numeric_and_unsortable_sizes(self):
        self.assertEqual(cards._size_label(['44', '38', '40']), 'Sizes 38-44')
        self.assertEqual(cards._size_label(['Small box', 'Big box']), '2 sizes')
        self.assertEqual(cards._size_label(['M']), 'Size M')

    def test_cached_and_forgotten_on_change(self):
        tee = self.product('Card Cache Tee', variant='Color')
        Variants.objects.create(product=tee, color=Color.objects.create(name='Red', code='#F00'), quantity=2)
        self.assertEqual(cards.card_extras([tee])[tee.pk]['color_count'], 1)
        with self.assertNumQueries(0):
            cards.card_extras([tee])
        Variants.objects.create(product=tee, color=Color.objects.create(name='Blue', code='#00F'), quantity=2)
        self.assertEqual(cards.card_extras([tee])[tee.pk]['color_count'], 2, "variant save drops the cached card")

    def test_category_page_carries_cards_with_bounded_queries(self):
        for i in range(12):
            p = self.product(f'Card Shirt {i}', variant='Color')
            Variants.objects.create(product=p, color=Color.objects.create(name=f'C{i}', code='#123456'), quantity=2)
        url = f'/api/v1/product/category/{self.sub.slug}/'
        APIClient().get(url)                      # warm the filter/facet caches
        cache.delete_many([f"{cards.CACHE_PREFIX}{p}" for p in Product.objects.values_list('pk', flat=True)])
        with CaptureQueriesContext(connection) as ctx:
            res = APIClient().get(url)
        self.assertEqual(res.status_code, 200)
        rows = res.data['products_with_details']
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(r['card']['color_count'] == 1 for r in rows))
        self.assertEqual(rows[0]['colors'][0]['color__code'], '#123456', "old swatch shape still served")
        self.assertLess(len(ctx.captured_queries), 40, "no per-product queries")

    def test_seller_storefront_and_brand_carry_cards(self):
        tee = self.product('Card Store Tee', variant='Color')
        Variants.objects.create(product=tee, color=Color.objects.create(name='Olive', code='#556B2F'), quantity=2)
        res = APIClient().get(f'/api/v1/vendor/seller-detail/{self.shop.slug}/products/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['results'][0]['card']['colors'][0]['name'], 'Olive')
        self.assertIn('sponsored', res.data)
        self.assertNotIn('vendor_id', res.data)
        res = APIClient().get(f'/api/v1/product/brand/{self.brand.slug}/')
        self.assertEqual(res.data['products_with_details'][0]['card']['colors'][0]['name'], 'Olive')

    def test_every_listing_has_bounded_queries(self):
        from product.models import Collection
        from recommendation.models import ProductDealScore
        for i in range(12):
            p = self.product(f'Card Bulk {i}', variant='Color')
            Variants.objects.create(product=p, color=Color.objects.create(name=f'B{i}', code='#222'), quantity=2)
            ProductDealScore.objects.create(product=p, discount_percent=20, best_price=Decimal('80'),
                                            compare_at_price=Decimal('100'), score=1, is_eligible=True)
        Collection.objects.create(title='Card Picks', slug='card-picks', is_active=True, filter_type='sub_category',
                                  sub_category=self.sub)
        urls = [f'/api/v1/product/category/{self.sub.slug}/', f'/api/v1/product/brand/{self.brand.slug}/',
                '/api/v1/product/collection/card-picks/', '/api/v1/recommendations/deals/',
                f'/api/v1/vendor/seller-detail/{self.shop.slug}/products/']
        for url in urls:
            APIClient().get(url)                     # warm page-independent caches
            cache.delete_many([f"{cards.CACHE_PREFIX}{p}" for p in Product.objects.values_list('pk', flat=True)])
            with CaptureQueriesContext(connection) as ctx:
                res = APIClient().get(url)
            self.assertEqual(res.status_code, 200, url)
            print(f"{url}: {len(ctx.captured_queries)} queries")
            self.assertLess(len(ctx.captured_queries), 40, f"{url} makes per-product queries")

"""
product/tests_curation.py
The automatic occasion/collection curator (product/curation.py).

Run with:  DB_HOST=localhost python manage.py test product.tests_curation --keepdb
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ecommerce.celery import app as celery_app
from product import curation
from address.models import Country
from product.models import (Category, Collection, Main_Category, Occasion, OccasionSection, Product,
                            SeasonalEvent, Sub_Category)
from userauths.models import User
from vendor.models import Vendor

celery_app.conf.task_always_eager = True

TEST_SETTINGS = {
    'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'CHANNEL_LAYERS': {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
}


@override_settings(**TEST_SETTINGS)
class CurationTestCase(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        for target in ('vendor.models.send_vendor_approval_email.delay', 'vendor.models.send_vendor_sms.delay',
                       'product.tasks.curate_collections.delay'):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

        self.vendors = []
        for n in range(4):
            user = User.objects.create_user(f'Cur{n}', 'Test', f'cur{n}@example.com', f'+23324200{n:04d}', 'Str0ng!Passw0rd')
            self.vendors.append(Vendor.objects.create(
                name=f'Curate Shop {n}', user=user, email=f'cur-shop{n}@example.com',
                contact=f'+23355000{n:04d}', is_approved=True, status='VERIFIED',
            ))
        main = Main_Category.objects.create(title='Cur Main')
        self.beauty = Category.objects.create(title='Cur Beauty', main_category=main)
        self.tech = Category.objects.create(title='Cur Tech', main_category=main)
        self.skin = Sub_Category.objects.create(title='Skin Care', category=self.beauty)
        self.hair = Sub_Category.objects.create(title='Hair Care', category=self.beauty)
        self.audio = Sub_Category.objects.create(title='Headphones', category=self.tech)
        self.n = 0
        self.now = timezone.now()

    def product(self, sub, vendor=None, price='100', old='100', qty=10, title=None, **kw):
        self.n += 1
        return Product.objects.create(
            title=title or f'{sub.title} Item {self.n}', sub_category=sub,
            vendor=vendor or self.vendors[self.n % len(self.vendors)], status='published',
            price=Decimal(price), old_price=Decimal(old), total_quantity=qty, image='p.jpg', **kw,
        )

    def auto_collection(self, slug='auto', **kw):
        defaults = dict(title=slug, slug=slug, filter_type='auto', auto_size=6)
        defaults.update(kw)
        return Collection.objects.create(**defaults)

    def curate(self, collection, **kw):
        collection.refresh_from_db()
        return curation.curate_collection(collection, curation.CandidatePool.load(self.now), self.now, **kw)

    # ── Palette ──

    def test_every_palette_colour_keeps_white_text_readable(self):
        for color in curation.PALETTE:
            self.assertGreaterEqual(curation.contrast_with_white(color), 4.5, color)

    # ── Collections ──

    def test_picks_only_eligible_products_in_scope(self):
        good = [self.product(self.skin) for _ in range(4)]
        self.product(self.audio)                                  # out of scope
        self.product(self.skin, qty=0)                            # sold out
        draft = self.product(self.skin); draft.status = 'draft'; draft.save()
        excluded = self.product(self.skin)
        c = self.auto_collection()
        c.auto_sub_categories.set([self.skin])
        c.excluded_products.set([excluded])

        chosen = self.curate(c)

        self.assertCountEqual(chosen, [p.id for p in good])
        self.assertEqual(list(c.get_products_qs().values_list('id', flat=True)), chosen)

    def test_min_discount_vendor_cap_and_pins(self):
        cheap = [self.product(self.skin, vendor=self.vendors[0], price='50', old='100') for _ in range(5)]
        for v in self.vendors[1:]:
            self.product(self.skin, vendor=v, price='70', old='100')
        self.product(self.skin, price='95', old='100')            # only 5% off
        pinned = self.product(self.skin, vendor=self.vendors[1], price='80', old='100')
        c = self.auto_collection(auto_min_discount=20, max_per_vendor=2, auto_size=5)
        c.pinned_products.set([pinned])

        chosen = self.curate(c)

        self.assertEqual(chosen[0], pinned.id)
        self.assertEqual(len(chosen), 5)
        self.assertLessEqual(sum(1 for pid in chosen if pid in {p.id for p in cheap}), 2)

    def test_vendor_cap_relaxes_rather_than_leave_empty_tiles(self):
        for _ in range(6):
            self.product(self.skin, vendor=self.vendors[0])
        c = self.auto_collection(max_per_vendor=2, auto_size=4)
        self.assertEqual(len(self.curate(c)), 4)

    def test_keywords_rank_matching_products_and_drop_misses(self):
        towel = self.product(self.skin, title='Bath Towels Set')
        self.product(self.skin, title='Face Serum')
        c = self.auto_collection(auto_keywords='towel, dorm')
        self.assertEqual(self.curate(c), [towel.id])

    def test_rotation_prefers_products_not_just_shown(self):
        for _ in range(12):
            self.product(self.skin)
        c = self.auto_collection(auto_size=6)
        first = self.curate(c)
        self.now += timedelta(days=3)
        second = self.curate(c)
        self.assertGreaterEqual(len(set(second) - set(first)), 4)
        c.refresh_from_db()
        self.assertEqual(c.recent_product_ids, first)

    def test_repair_between_rotations_swaps_only_dead_products(self):
        for _ in range(10):
            self.product(self.skin)
        c = self.auto_collection(auto_size=6, rotation_days=3)
        first = self.curate(c)
        Product.objects.filter(id=first[2]).update(total_quantity=0)
        self.now += timedelta(days=1)                             # not due yet

        second = self.curate(c)

        self.assertNotIn(first[2], second)
        self.assertEqual(second[:5], [pid for pid in first if pid != first[2]])
        self.assertEqual(len(second), 6)

    def test_untouched_when_nothing_to_repair(self):
        for _ in range(8):
            self.product(self.skin)
        c = self.auto_collection()
        first = self.curate(c)
        self.now += timedelta(days=1)
        self.assertEqual(self.curate(c), first)

    # ── Occasions ──

    def test_occasion_cards_never_repeat_a_product(self):
        for _ in range(10):
            self.product(self.skin)
        occasion = Occasion.objects.create(title='School', slug='school')
        for i in range(2):
            c = self.auto_collection(slug=f'card{i}', auto_size=6)
            c.auto_sub_categories.set([self.skin])
            OccasionSection.objects.create(occasion=occasion, title=f'Card {i}', collection=c, position=i)

        curation.run(now=self.now)

        a, b = (Collection.objects.get(slug=f'card{i}').curated_product_ids for i in range(2))
        self.assertEqual(len(a), 6)
        self.assertFalse(set(a) & set(b))

    def make_catalogue(self):
        for _ in range(9):
            self.product(self.skin, price='60', old='100')        # 40% off
        for _ in range(9):
            self.product(self.hair)
        for _ in range(9):
            self.product(self.audio)
        Product.objects.filter(sub_category=self.audio).update(trending_score=5.0)

    def test_auto_sections_builds_titled_staggered_cards(self):
        self.make_catalogue()
        occasion = Occasion.objects.create(title='Big Week', slug='big-week', auto_sections=True,
                                           auto_section_count=3, rotation_days=3, color_mode='mixed')

        curation.run(now=self.now)

        sections = list(occasion.sections.select_related('collection').order_by('position'))
        self.assertEqual(len(sections), 3)
        titles = {s.title for s in sections}
        self.assertIn('Skin Care up to 40% off', titles)
        self.assertIn('Trending in Headphones', titles)
        for s in sections:
            self.assertTrue(s.is_generated)
            self.assertEqual(s.collection.title, s.title)
            self.assertGreaterEqual(len(s.collection.curated_product_ids), 8)
        rotations = {s.collection.next_rotation_at.date() for s in sections}
        self.assertEqual(len(rotations), 3, 'each card should rotate on a different day')
        colors = [s.accent_color for s in sections]
        self.assertTrue(all(colors) and colors[0] != colors[1] != colors[2])

    def test_auto_sections_keep_hand_made_cards_and_stable_slugs(self):
        self.make_catalogue()
        manual = Collection.objects.create(title='Mine', slug='mine')
        occasion = Occasion.objects.create(title='Mix', slug='mix', auto_sections=True, auto_section_count=3)
        OccasionSection.objects.create(occasion=occasion, title='Mine', collection=manual, position=0)

        curation.run(now=self.now)
        slugs = list(occasion.sections.order_by('position').values_list('collection__slug', flat=True))
        self.now += timedelta(days=1)
        curation.run(now=self.now)

        self.assertEqual(slugs[0], 'mine')
        self.assertEqual(len(slugs), 3)
        self.assertEqual(list(occasion.sections.order_by('position').values_list('collection__slug', flat=True)), slugs)

    def test_rotate_colour_mode_changes_whole_row_on_schedule(self):
        self.make_catalogue()
        occasion = Occasion.objects.create(title='Row', slug='row', auto_sections=True, auto_section_count=2,
                                           rotation_days=3, color_mode='rotate')
        curation.run(now=self.now)
        occasion.refresh_from_db()
        first = occasion.accent_color
        self.assertIn(first, curation.PALETTE)
        self.assertEqual(set(occasion.sections.values_list('accent_color', flat=True)), {''})

        self.now += timedelta(days=1)
        curation.run(now=self.now)
        occasion.refresh_from_db()
        self.assertEqual(occasion.accent_color, first)

        self.now += timedelta(days=2)
        curation.run(now=self.now)
        occasion.refresh_from_db()
        self.assertNotEqual(occasion.accent_color, first)

    # ── API ──

    def test_api_serves_card_colours_and_curated_order(self):
        self.make_catalogue()
        occasion = Occasion.objects.create(title='Api', slug='api', auto_sections=True, auto_section_count=2,
                                           color_mode='mixed')
        curation.run(now=self.now)
        client = APIClient()

        data = client.get('/api/v1/product/occasions/').json()
        cards = next(o for o in data if o['slug'] == 'api')['sections']
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(card['accent_color'] in curation.PALETTE for card in cards))
        self.assertEqual(len(cards[0]['products']), 4)

        slug = cards[0]['collection_slug']
        page = client.get(f'/api/v1/product/collection/{slug}/').json()
        expected = Collection.objects.get(slug=slug).curated_product_ids[:12]
        self.assertEqual([p['product']['id'] for p in page['products_with_details']], expected)

    # ── Holidays ──

    def test_holiday_rules_place_the_right_dates(self):
        from datetime import date
        mothers = SeasonalEvent(rule='nth_weekday', month=5, weekday=6, nth=2)
        black_friday = SeasonalEvent(rule='nth_weekday', month=11, weekday=3, nth=4, offset_days=1)
        good_friday = SeasonalEvent(rule='easter', offset_days=-2)
        last_monday = SeasonalEvent(rule='nth_weekday', month=5, weekday=0, nth=-1)
        eid = SeasonalEvent(rule='dates', dates=['2027-03-10', 'junk'])
        leap = SeasonalEvent(rule='fixed', month=2, day=29)

        self.assertEqual(mothers.date_in_year(2027), date(2027, 5, 9))
        self.assertEqual(black_friday.date_in_year(2026), date(2026, 11, 27))
        self.assertEqual(good_friday.date_in_year(2027), date(2027, 3, 26))
        self.assertEqual(last_monday.date_in_year(2027), date(2027, 5, 31))
        self.assertEqual(eid.date_in_year(2027), date(2027, 3, 10))
        self.assertIsNone(eid.date_in_year(2028))
        self.assertIsNone(leap.date_in_year(2027))

    def test_holiday_window_spans_new_year(self):
        from datetime import date
        new_year = SeasonalEvent(rule='fixed', month=1, day=1, lead_days=10, days_after=2)
        self.assertEqual(new_year.window_for(date(2026, 12, 25))[1], date(2027, 1, 1))
        self.assertEqual(new_year.window_for(date(2027, 1, 3))[1], date(2027, 1, 1))
        self.assertIsNone(new_year.window_for(date(2027, 1, 4)))

    def test_seeded_calendar_is_off_until_enabled(self):
        self.assertTrue(SeasonalEvent.objects.filter(slug='christmas').exists())
        self.assertFalse(SeasonalEvent.objects.filter(is_enabled=True).exists())

    def test_holiday_in_season_opens_its_own_occasion_once(self):
        self.make_catalogue()
        event = SeasonalEvent.objects.create(
            title='Glow Week', slug='glow-week', rule='fixed', month=self.now.month, day=self.now.day,
            lead_days=5, days_after=1, section_count=2, color_mode='mixed', keywords='skin',
        )
        SeasonalEvent.objects.create(title='Far Off', slug='far-off', rule='dates', dates=['2099-01-01'])

        curation.run(now=self.now)
        curation.run(now=self.now)

        occasion = Occasion.objects.get(event=event)
        self.assertEqual(occasion.slug, f'glow-week-{self.now.year}')
        self.assertEqual(occasion.end_date, self.now.date() + timedelta(days=1))
        self.assertTrue(occasion.auto_sections)
        self.assertEqual(Occasion.objects.filter(event__isnull=False).count(), 1)
        titles = list(occasion.sections.values_list('title', flat=True))
        self.assertTrue(titles and all('Skin Care' in t for t in titles), titles)

    # ── Countries ──

    def test_country_occasion_reaches_only_that_country_with_products_that_ship_there(self):
        self.make_catalogue()
        ghana = Country.objects.get_or_create(code='GH', defaults={'name': 'Ghana'})[0]
        nigeria = Country.objects.get_or_create(code='NG', defaults={'name': 'Nigeria'})[0]
        ng_only = list(Product.objects.filter(sub_category=self.skin)[:3])
        for p in ng_only:
            p.available_in_regions.set([nigeria])
        occasion = Occasion.objects.create(title='Ghana Only', slug='gh-only', auto_sections=True,
                                           auto_section_count=3)
        occasion.countries.set([ghana])

        curation.run(now=self.now)

        picked = {pid for c in Collection.objects.filter(occasion_sections__occasion=occasion)
                  for pid in c.curated_product_ids}
        self.assertTrue(picked)
        self.assertFalse(picked & {p.id for p in ng_only})

        client = APIClient()
        slugs = lambda **h: [o['slug'] for o in client.get('/api/v1/product/occasions/', **h).json()]
        self.assertIn('gh-only', slugs())                                  # default country is GH
        self.assertIn('gh-only', slugs(HTTP_X_COUNTRY='gh'))
        self.assertNotIn('gh-only', slugs(HTTP_X_COUNTRY='NG'))
        self.assertNotIn('gh-only', slugs(HTTP_CF_IPCOUNTRY='US'))

    def test_storefront_hides_products_that_do_not_ship_to_the_shopper(self):
        for _ in range(5):
            self.product(self.skin)
        nigeria = Country.objects.get_or_create(code='NG', defaults={'name': 'Nigeria'})[0]
        c = self.auto_collection(slug='global')
        self.curate(c)
        c.refresh_from_db()
        ng_only = Product.objects.get(id=c.curated_product_ids[0])
        ng_only.available_in_regions.set([nigeria])

        client = APIClient()
        ids = lambda country: [p['product']['id'] for p in client.get(
            '/api/v1/product/collection/global/', HTTP_X_COUNTRY=country).json()['products_with_details']]
        self.assertNotIn(ng_only.id, ids('GH'))
        self.assertIn(ng_only.id, ids('NG'))
        self.assertEqual(len(ids('NG')), 5)


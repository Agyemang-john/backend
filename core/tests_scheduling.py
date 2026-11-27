"""
core/tests_scheduling.py
Hero slides and side banners follow seasons; between seasons the everyday ones show.

Run with:  DB_HOST=localhost python manage.py test core.tests_scheduling --keepdb
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from address.models import Country
from core.models import Banners, HomeSlider
from product.models import Occasion, SeasonalEvent

SLIDERS = '/api/sliders/'
BANNERS = '/api/banners/'


class SchedulingTests(TestCase):

    def setUp(self):
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1, 'USD': 0.1}, None)
        self.client = APIClient()
        self.today = timezone.localdate()
        self.everyday = HomeSlider.objects.create(title='Everyday deals', order=1)
        self.kept = HomeSlider.objects.create(title='Free delivery', order=2, keep_in_season=True)

    def titles(self, url=SLIDERS, **headers):
        res = self.client.get(url, **headers)
        self.assertEqual(res.status_code, 200)
        return [s['title'] for s in res.data]

    def christmas(self, **kw):
        event = SeasonalEvent.objects.create(title='Christmas gifts', slug='christmas', rule='fixed', month=12, day=25)
        occasion = Occasion.objects.create(
            title='Christmas gifts', slug=f'christmas-{self.today.year}', event=event, accent_color='#B71C1C',
            start_date=self.today - timedelta(days=3), end_date=self.today + timedelta(days=3), **kw)
        return event, occasion

    def test_between_seasons_the_everyday_slides_show(self):
        event = SeasonalEvent.objects.create(title='Christmas gifts', slug='christmas', rule='fixed', month=12, day=25)
        HomeSlider.objects.create(title='Christmas', order=0, holiday=event)       # no occasion open
        HomeSlider.objects.create(title='Old sale', order=0, ends_at=timezone.now() - timedelta(hours=1))
        HomeSlider.objects.create(title='Next sale', order=0, starts_at=timezone.now() + timedelta(days=1))
        HomeSlider.objects.create(title='Switched off', order=0, is_active=False)
        self.assertEqual(self.titles(), ['Everyday deals', 'Free delivery'])

    def test_a_holiday_slide_takes_over_every_year_with_its_colour(self):
        event, occasion = self.christmas()
        HomeSlider.objects.create(title='Christmas', order=5, holiday=event)
        res = self.client.get(SLIDERS)
        self.assertEqual([s['title'] for s in res.data], ['Christmas', 'Free delivery'],
                         'seasonal first; everyday ones step aside unless kept')
        self.assertEqual(res.data[0]['season']['accent_color'], '#B71C1C')
        self.assertIsNone(res.data[1]['season'])

        Occasion.objects.filter(pk=occasion.pk).update(end_date=self.today - timedelta(days=1))
        cache.clear()
        cache.set('exchange_rates', {'GHS': 1}, None)
        self.assertEqual(self.titles(), ['Everyday deals', 'Free delivery'], 'back to everyday after the season')

    def test_occasions_respect_the_shoppers_country(self):
        ghana, _ = Country.objects.get_or_create(name='Ghana', defaults={'code': 'GH'})
        _, occasion = self.christmas()
        occasion.countries.set([ghana])
        HomeSlider.objects.create(title='Christmas', occasion=occasion)
        self.assertEqual(self.titles(HTTP_X_COUNTRY=ghana.code)[0], 'Christmas')
        self.assertNotIn('Christmas', self.titles(HTTP_X_COUNTRY='NG'))

    def test_dated_slides_show_inside_their_window(self):
        HomeSlider.objects.create(title='Weekend sale', starts_at=timezone.now() - timedelta(hours=1),
                                  ends_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.titles(), ['Weekend sale', 'Free delivery'])

    def test_banners_follow_the_same_rules_and_hide_switched_off_ones(self):
        event, _ = self.christmas()
        Banners.objects.create(title='Everyday banner', link='/deals')
        Banners.objects.create(title='Hidden', link='/x', is_active=False)
        Banners.objects.create(title='Christmas banner', link='/c', holiday=event)
        self.assertEqual(self.titles(BANNERS), ['Christmas banner'])

    def test_saving_a_slide_shows_it_straight_away(self):
        self.titles()                                               # fills the cache
        HomeSlider.objects.create(title='New everyday', order=3)
        self.assertIn('New everyday', self.titles())

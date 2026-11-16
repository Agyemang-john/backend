"""Tests for product/search_trends.py (search bar trending + popular searches)."""
import uuid
from datetime import timedelta

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from product import search_trends as st
from product.models import BlockedSearchTerm, SearchTermDaily

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36'


@override_settings(CACHES=LOCMEM)
class SearchTrendsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()

    def _req(self, visitor=None, ua=BROWSER_UA):
        req = self.rf.get('/', HTTP_USER_AGENT=ua, HTTP_X_VISITOR_ID=visitor or str(uuid.uuid4()))
        from django.contrib.auth.models import AnonymousUser
        req.user = AnonymousUser()
        return req

    def _searched_by(self, term, n, days_ago=0):
        SearchTermDaily.objects.update_or_create(
            term=term, date=timezone.localdate() - timedelta(days=days_ago), defaults={'searchers': n})

    # ── normalising ──────────────────────────────────────────────────────────
    def test_normalize(self):
        self.assertEqual(st.normalize_term('  Apple   iPhone 17! '), 'apple iphone 17')
        for junk in ['a', '1234', 'me@mail.com', 'www.site.com', 'call 024 123 4567', '', None, 'x' * 90]:
            self.assertIsNone(st.normalize_term(junk), junk)

    # ── recording ────────────────────────────────────────────────────────────
    def test_one_count_per_visitor_per_day(self):
        vid = str(uuid.uuid4())
        st.record_search(self._req(vid), 'Rice Cooker', 5)
        st.record_search(self._req(vid), 'rice   cooker', 5)
        st.record_search(self._req(), 'rice cooker', 5)
        self.assertEqual(SearchTermDaily.objects.get(term='rice cooker').searchers, 2)

    def test_no_results_and_bots_not_counted(self):
        st.record_search(self._req(), 'zzqx gadget', 0)
        st.record_search(self._req(ua='Googlebot/2.1'), 'rice cooker', 5)
        self.assertFalse(SearchTermDaily.objects.exists())

    # ── trending ─────────────────────────────────────────────────────────────
    def test_trending_needs_several_shoppers_and_ranks_recent_higher(self):
        self._searched_by('one person query', st.MIN_SEARCHERS - 1)
        self._searched_by('old favourite', 10, days_ago=6)
        self._searched_by('hot today', 8)
        self._searched_by('way too old', 50, days_ago=st.WINDOW_DAYS)
        trending = st.trending_searches()
        self.assertEqual(trending[:2], ['hot today', 'old favourite'])
        self.assertNotIn('one person query', trending)
        self.assertNotIn('way too old', trending)

    def test_blocked_terms_hidden(self):
        self._searched_by('nice shoes', 9)
        self._searched_by('bad word here', 9)
        BlockedSearchTerm.objects.create(term='  Bad Word ')
        self.assertEqual(st.trending_searches(), ['nice shoes'])

    def test_completions(self):
        self._searched_by('iphone 17 pro max', 9)
        self._searched_by('apple iphone 16', 5)
        self._searched_by('iphone', 20)
        self.assertEqual(st.complete_query('IPH'), ['iphone', 'iphone 17 pro max', 'apple iphone 16'])
        self.assertEqual(st.complete_query('iphone'), ['iphone 17 pro max', 'apple iphone 16'])
        self.assertEqual(st.complete_query('i'), [])

    def test_endpoints(self):
        self._searched_by('wireless earbuds', 4)
        client = APIClient()
        self.assertEqual(client.get('/api/v1/product/trending-searches/').json()['trending'], ['wireless earbuds'])
        self.assertEqual(client.get('/api/v1/product/search-completions/?q=wire').json()['queries'], ['wireless earbuds'])

    def test_purge(self):
        self._searched_by('ancient', 5, days_ago=st.KEEP_DAYS + 1)
        self._searched_by('fresh', 5)
        st.purge_old_terms()
        self.assertEqual(list(SearchTermDaily.objects.values_list('term', flat=True)), ['fresh'])

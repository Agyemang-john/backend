"""
tools/demand.py
"What shoppers want": demand from on-site search, for sellers.

refresh() (nightly) condenses the last 28 days of product.SearchTermDaily into
tools.DemandTerm: per popular term, shoppers who found something, shoppers who
found nothing, how many live listings match now, and the category most of
them are in (or, with no matches, the category whose name fits the term).
Same privacy rules as the search bar: a term needs MIN_SEARCHERS different
shoppers and blocked terms never show.

for_vendor() reads that table for one store's categories:
  - opportunities: searched a lot, few listings: demand per listing, highest first;
  - nothing found: searches with no matching listing at all;
  - your share: how many of the matching listings are yours.
"""
from datetime import timedelta

from django.contrib.postgres.search import SearchQuery
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from product.models import SEARCH_CONFIG, BlockedSearchTerm, Product, SearchTermDaily, Sub_Category
from product.search_trends import MIN_SEARCHERS

WINDOW_DAYS = 28
MAX_TERMS = 2000
CACHE_SECONDS = 3600
SHOW = 25
VERSION_KEY = 'tools:demand:version'


def _category_by_words():
    """{word: sub_category_id} from category names, for terms nothing matches yet."""
    index = {}
    for sc in Sub_Category.objects.only('pk', 'title'):
        for word in (sc.title or '').lower().replace('&', ' ').replace(',', ' ').split():
            if len(word) > 3:
                index.setdefault(word.rstrip('s'), sc.pk)
    return index


def refresh(now=None):
    """Rebuild DemandTerm. One full-text count per popular term (GIN index)."""
    from tools.models import DemandTerm

    today = (now or timezone.now()).date()
    blocked = [b for b in BlockedSearchTerm.objects.values_list('term', flat=True) if b]
    rows = (SearchTermDaily.objects.filter(date__gte=today - timedelta(days=WINDOW_DAYS - 1))
            .values('term').annotate(found=Sum('searchers'), none=Sum('no_result_searchers')))
    rows = sorted((r for r in rows if (r['found'] or 0) + (r['none'] or 0) >= MIN_SEARCHERS
                   and not any(b in r['term'] for b in blocked)),
                  key=lambda r: -((r['found'] or 0) + (r['none'] or 0)))[:MAX_TERMS]
    words = _category_by_words()
    terms = []
    for r in rows:
        matches = list(Product.published.filter(search_vector=SearchQuery(r['term'], config=SEARCH_CONFIG),
                                                is_buybox_winner=True)
                       .values('sub_category_id').annotate(n=Count('id')).order_by('-n'))
        live = sum(m['n'] for m in matches)
        category = matches[0]['sub_category_id'] if matches else next(
            (words[w.rstrip('s')] for w in r['term'].split() if w.rstrip('s') in words), None)
        terms.append(DemandTerm(term=r['term'], searchers=r['found'] or 0, no_result_searchers=r['none'] or 0,
                                live_results=live, sub_category_id=category))
    with transaction.atomic():
        DemandTerm.objects.all().delete()
        DemandTerm.objects.bulk_create(terms, batch_size=500)
    cache.set(VERSION_KEY, timezone.now().timestamp(), None)   # every store's cached view is now stale
    return len(terms)


def _yours(vendor, term):
    return Product.published.filter(vendor=vendor, search_vector=SearchQuery(term, config=SEARCH_CONFIG)).count()


def for_vendor(vendor):
    from tools.models import DemandTerm

    key = f'tools:demand:{cache.get(VERSION_KEY, 0)}:{vendor.pk}'
    found = cache.get(key)
    if found is not None:
        return found
    categories = set(Product.objects.filter(vendor=vendor).exclude(sub_category__isnull=True)
                     .values_list('sub_category_id', flat=True).distinct())
    mine = list(DemandTerm.objects.filter(sub_category_id__in=categories).select_related('sub_category'))

    def row(t, with_share=False):
        item = {'term': t.term, 'searchers': t.total, 'found_nothing': t.no_result_searchers,
                'live_results': t.live_results, 'category': t.sub_category.title if t.sub_category_id else None,
                'per_listing': round(t.total / (t.live_results or 1), 1)}
        if with_share:
            item['yours'] = _yours(vendor, t.term)
        return item

    opportunities = sorted((t for t in mine if t.live_results > 0),
                           key=lambda t: (-(t.total / t.live_results), -t.total))[:SHOW]
    nothing = sorted((t for t in mine if t.live_results == 0), key=lambda t: -t.total)[:SHOW]
    if len(nothing) < SHOW:      # topped up with searches nothing on the site answers, any category
        extra = (DemandTerm.objects.filter(live_results=0).exclude(pk__in=[t.pk for t in nothing])
                 .select_related('sub_category').order_by('-no_result_searchers')[:SHOW - len(nothing)])
        nothing += list(extra)
    biggest = sorted((t for t in mine if t.live_results > 0), key=lambda t: -t.total)[:SHOW]

    result = {
        'window_days': WINDOW_DAYS,
        'updated_at': max((t.updated_at for t in mine), default=None),
        'opportunities': [row(t) for t in opportunities],
        'nothing_found': [row(t) for t in nothing],
        'your_share': [row(t, with_share=True) for t in biggest],
    }
    if result['updated_at']:
        result['updated_at'] = result['updated_at'].isoformat()
    cache.set(key, result, CACHE_SECONDS)
    return result

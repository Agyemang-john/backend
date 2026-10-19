"""
recommendation/deals_page.py

The full "Today's Deals" page — every live deal, filterable and sortable, the
way a large storefront's deals hub works: department chips with counts, a
discount-depth filter, flash deals, price/brand/seller/rating filters, and a
default "Best deals" order that is the deal model's ranking, personalised and
diversified.

The eligible deal set is small next to the catalog (a few thousand rows at
most) and changes only when deals are rescored, so it is loaded once per
scoring run into a flat, cached list of facts and filtered in Python. That keeps
every facet count consistent with the result list — both are computed from the
same rows — and costs one cache read per request instead of a join per filter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from .models import ProductDealScore

PAGE_SIZE = 24

#: Discount-depth filter options, in percent.
DISCOUNT_STEPS = (10, 25, 50)

#: Collection filter type whose collection supplies the page banner.
DEALS_COLLECTION_TYPE = 'todays_deals'

#: Diversity caps on each page of the default sort.
PAGE_MAX_PER_VENDOR = 4
PAGE_MAX_PER_SUB_CATEGORY = 6

SORTS = {
    'best':        'Best deals',
    'discount':    'Biggest discount',
    'ending_soon': 'Ending soon',
    'price_asc':   'Price: low to high',
    'price_desc':  'Price: high to low',
    'rating':      'Highest rated',
    'newest':      'Newest',
}


@dataclass
class DealFilters:
    department: int | None = None        # Main_Category id
    category: int | None = None          # Category id
    brands: list[int] = field(default_factory=list)
    vendors: list[int] = field(default_factory=list)
    rating: int | None = None
    min_discount: int | None = None
    flash_only: bool = False
    price_from: float | None = None      # GHS
    price_to: float | None = None        # GHS
    sort: str = 'best'
    page: int = 1

    @classmethod
    def from_query(cls, params, rate: Decimal) -> 'DealFilters':
        def one_int(name):
            value = params.get(name)
            return int(value) if value and str(value).isdigit() else None

        def many_int(name):
            return [int(v) for v in params.getlist(name) if str(v).isdigit()]

        def price(name):
            value = params.get(name)
            try:
                return float(Decimal(str(value)) / rate) if value not in (None, '') else None
            except Exception:
                return None

        sort = params.get('sort') or 'best'
        return cls(
            department=one_int('department'),
            category=one_int('category'),
            brands=many_int('brand'),
            vendors=many_int('vendor'),
            rating=min(many_int('rating') or [0]) or None,
            min_discount=one_int('discount'),
            flash_only=params.get('flash') in ('1', 'true'),
            price_from=price('from'),
            price_to=price('to'),
            sort=sort if sort in SORTS else 'best',
            page=max(1, one_int('page') or 1),
        )


def _deal_rows() -> list[dict]:
    """Every eligible deal with the facts the page filters and sorts on. Cached per scoring run."""
    from product.models import FlashSale

    from .serving import deals_version

    cache_key = f"rec:deals_page:{deals_version()}"
    rows = cache.get(cache_key)
    if rows is not None:
        return rows

    now = timezone.now()
    flash_end: dict[int, datetime] = {}
    for product_id, end_time in (
        FlashSale.objects
        .filter(is_active=True, start_time__lte=now, end_time__gte=now, product__isnull=False)
        .values_list('product_id', 'end_time')
    ):
        if product_id not in flash_end or end_time < flash_end[product_id]:
            flash_end[product_id] = end_time

    queryset = (
        ProductDealScore.objects
        .filter(
            is_eligible=True,
            product__status='published',
            product__vendor__shop_paused=False,
            product__vendor__is_suspended=False,
        )
        .values_list(
            'product_id', 'score', 'discount_percent', 'best_price', 'has_flash_sale',
            'product__vendor_id', 'product__brand_id', 'product__sub_category_id',
            'product__sub_category__category_id',
            'product__sub_category__category__main_category_id',
            'product__avg_rating', 'product__date',
        )
    )
    rows = []
    for (product_id, score, discount, best_price, has_flash, vendor, brand, sub_category,
         category, department, rating, created) in queryset:
        end = flash_end.get(product_id) if has_flash else None
        rows.append({
            'id': product_id,
            'score': float(score or 0.0),
            'discount': float(discount or 0.0),
            'price': float(best_price or 0),
            'flash_end': end.isoformat() if end else None,
            'vendor': vendor,
            'brand': brand,
            'sub_category': sub_category,
            'category': category,
            'department': department,
            'rating': float(rating or 0.0),
            'created': created.timestamp() if created else 0.0,
        })
    rows.sort(key=lambda row: -row['score'])
    cache.set(cache_key, rows, 3600)
    return rows


def _matches(row: dict, f: DealFilters, skip: str = '') -> bool:
    """Whether a deal passes every filter except `skip` (used for facet counts)."""
    if skip != 'department' and f.department and row['department'] != f.department:
        return False
    if skip != 'category' and f.category and row['category'] != f.category:
        return False
    if skip != 'discount' and f.min_discount and row['discount'] < f.min_discount:
        return False
    if skip != 'flash' and f.flash_only and not row['flash_end']:
        return False
    if f.brands and row['brand'] not in f.brands:
        return False
    if f.vendors and row['vendor'] not in f.vendors:
        return False
    if f.rating and row['rating'] < f.rating:
        return False
    if f.price_from is not None and row['price'] < f.price_from:
        return False
    if f.price_to is not None and row['price'] > f.price_to:
        return False
    return True


def _sorted_ids(request, rows: list[dict], sort: str) -> list[int]:
    if sort == 'discount':
        rows = sorted(rows, key=lambda r: (-r['discount'], -r['score']))
    elif sort == 'ending_soon':
        # Flash deals by end time first, then everything else by deal quality.
        rows = sorted(rows, key=lambda r: (r['flash_end'] is None, r['flash_end'] or '', -r['score']))
    elif sort == 'price_asc':
        rows = sorted(rows, key=lambda r: (r['price'], -r['score']))
    elif sort == 'price_desc':
        rows = sorted(rows, key=lambda r: (-r['price'], -r['score']))
    elif sort == 'rating':
        rows = sorted(rows, key=lambda r: (-r['rating'], -r['score']))
    elif sort == 'newest':
        rows = sorted(rows, key=lambda r: -r['created'])
    else:
        from .serving import diversify, personalize_deals

        facts = {r['id']: (r['vendor'], r['sub_category'], r['category']) for r in rows}
        ranked = personalize_deals(request, [(r['id'], r['score']) for r in rows], facts)
        # Diversify page by page, so every page — not only the first — is varied.
        ordered: list[int] = []
        remaining = ranked
        while remaining:
            page = diversify(
                remaining, facts, PAGE_SIZE,
                max_per_vendor=PAGE_MAX_PER_VENDOR,
                max_per_sub_category=PAGE_MAX_PER_SUB_CATEGORY,
            )
            ordered.extend(page)
            taken = set(page)
            remaining = [pid for pid in remaining if pid not in taken]
        return ordered
    return [r['id'] for r in rows]


def _names(model, ids, *fields) -> list[dict]:
    if not ids:
        return []
    return list(model.objects.filter(id__in=ids).values('id', *fields))


def deals_page(request, rate: Decimal) -> dict:
    """
    One page of deals plus everything the filter UI needs. Prices in the facets
    are GHS; the view converts them alongside the product cards.
    """
    from product.models import Brand, Category, Collection, Main_Category
    from vendor.models import Vendor

    from .serving import _dismissed_ids

    f = DealFilters.from_query(request.query_params, rate)
    hidden = _dismissed_ids(request)
    rows = [row for row in _deal_rows() if row['id'] not in hidden]

    matched = [row for row in rows if _matches(row, f)]
    ids = _sorted_ids(request, matched, f.sort)

    total = len(ids)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(f.page, pages)
    page_ids = ids[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    # ── Facets ───────────────────────────────────────────────────────────────
    department_counts: dict[int, int] = {}
    for row in rows:
        if row['department'] and _matches(row, f, skip='department'):
            department_counts[row['department']] = department_counts.get(row['department'], 0) + 1

    category_counts: dict[int, int] = {}
    if f.department:
        for row in rows:
            if row['category'] and _matches(row, f, skip='category'):
                category_counts[row['category']] = category_counts.get(row['category'], 0) + 1

    discount_pool = [row for row in rows if _matches(row, f, skip='discount')]
    discount_buckets = [
        {'value': step, 'count': sum(1 for row in discount_pool if row['discount'] >= step)}
        for step in DISCOUNT_STEPS
    ]
    flash_count = sum(1 for row in rows if row['flash_end'] and _matches(row, f, skip='flash'))
    all_departments_count = sum(1 for row in rows if _matches(row, f, skip='department'))

    departments = sorted(
        ({**d, 'count': department_counts[d['id']]}
         for d in _names(Main_Category, list(department_counts), 'title', 'slug')),
        key=lambda d: -d['count'],
    )
    categories = sorted(
        ({**c, 'count': category_counts[c['id']]}
         for c in _names(Category, list(category_counts), 'title', 'slug')),
        key=lambda c: -c['count'],
    )

    prices = [row['price'] for row in rows]
    collection = (
        Collection.objects.filter(filter_type=DEALS_COLLECTION_TYPE, is_active=True)
        .order_by('-updated_at').first()
    )

    return {
        'page_ids': page_ids,
        'flash_end': {row['id']: row['flash_end'] for row in matched if row['id'] in set(page_ids)},
        'total': total,
        'page': page,
        'pages': pages,
        'sort': f.sort,
        'sorts': [{'value': key, 'label': label} for key, label in SORTS.items()],
        'departments': departments,
        'all_count': all_departments_count,
        'categories': categories,
        'discount_buckets': discount_buckets,
        'flash_count': flash_count,
        'brands': _names(Brand, {row['brand'] for row in rows if row['brand']}, 'title', 'slug'),
        'vendors': _names(Vendor, {row['vendor'] for row in rows if row['vendor']}, 'name', 'slug'),
        'min_price': min(prices) if prices else 0.0,
        'max_price': max(prices) if prices else 0.0,
        'collection': collection,
    }

"""
recommendation/shelves.py

Category shelves — "Best sellers in Phones", "Trending in Fashion".

A single global trending list stops working as the catalog widens: the top of it
is whichever department sells most, and a shopper who only buys baby clothes
never sees the best of anything they care about. Large storefronts solve this by
merchandising per department, then choosing *which* departments to show from
what the shopper browses. That is all this module does:

  1. rank products inside each category — best sellers by units sold in the
     last 30 days, trending by the existing engagement score — cached per
     category, so a shelf is one cache read after the first request;
  2. pick the shopper's categories from their recent views, cart and orders,
     topped up with the marketplace's busiest categories so a first-time
     visitor still gets a full page.

Shelves use the middle level of the category tree (Category, served at /cp/<slug>):
the top level is too broad to feel curated and the bottom level too narrow to
fill a shelf.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

SHELF_CACHE_TTL = 3600
BEST_SELLER_DAYS = 30

#: A best-seller shelf needs at least this many products with sales; otherwise
#: the category shows its trending shelf instead.
MIN_BEST_SELLERS = 4

KIND_BEST_SELLERS = 'best_sellers'
KIND_TRENDING = 'trending'


def _ranking_version() -> int:
    """Shelves follow the deals clock — both are rescored by the hourly task."""
    from .serving import deals_version
    return deals_version()


def category_ranking(category_id: int, kind: str, limit: int = 40) -> list[int]:
    """Product ids for one category shelf, best first. Cached per category."""
    from order.models import OrderProduct
    from product.models import Product

    cache_key = f"rec:shelf:{_ranking_version()}:{kind}:{category_id}"
    ids = cache.get(cache_key)
    if ids is not None:
        return ids[:limit]

    published = Product.published.filter(sub_category__category_id=category_id)

    if kind == KIND_BEST_SELLERS:
        since = timezone.now() - timedelta(days=BEST_SELLER_DAYS)
        ids = list(
            OrderProduct.objects
            .filter(
                date_created__gte=since, order__is_ordered=True,
                product__in=published,
            )
            .values('product_id')
            .annotate(units=Sum('quantity'), orders=Count('order_id', distinct=True))
            .order_by('-orders', '-units')
            .values_list('product_id', flat=True)[:60]
        )
    else:
        ids = list(
            published.order_by('-trending_score', '-avg_rating', '-views')
            .values_list('id', flat=True)[:60]
        )

    cache.set(cache_key, ids, SHELF_CACHE_TTL)
    return ids[:limit]


def _popular_categories(limit: int) -> list[int]:
    """The marketplace's busiest categories — the cold-start shelf order."""
    from product.models import Category, Product

    cache_key = f"rec:shelf:{_ranking_version()}:popular_categories"
    ids = cache.get(cache_key)
    if ids is None:
        counts = (
            Product.published
            .filter(sub_category__category__isnull=False)
            .values('sub_category__category_id')
            .annotate(n=Count('id'))
        )
        sizes = {row['sub_category__category_id']: row['n'] for row in counts}
        engagement = dict(
            Category.objects.filter(id__in=sizes).values_list('id', 'engagement_score')
        )
        # Engagement first, catalog depth as the tie-breaker — a busy category
        # with eight products makes a thin shelf.
        ids = sorted(sizes, key=lambda cid: (-(engagement.get(cid) or 0.0), -sizes[cid]))
        cache.set(cache_key, ids, SHELF_CACHE_TTL)
    return ids[:limit]


def shopper_categories(request, limit: int = 4) -> list[int]:
    """
    Category ids ordered by this shopper's interest: recent views, then cart and
    orders weighted higher, then the platform's popular categories.
    """
    from product.models import Product

    from .serving import _cart_product_ids, _recent_product_ids

    weights: dict[int, float] = defaultdict(float)

    recent = _recent_product_ids(request, limit=30)
    cart = _cart_product_ids(request)
    ordered: list[int] = []
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        from order.models import OrderProduct
        ordered = list(
            OrderProduct.objects
            .filter(order__user=user, order__is_ordered=True)
            .order_by('-date_created')
            .values_list('product_id', flat=True)[:30]
        )

    touched = set(recent) | set(cart) | set(ordered)
    if touched:
        category_of = dict(
            Product.objects.filter(id__in=touched, sub_category__category__isnull=False)
            .values_list('id', 'sub_category__category_id')
        )
        for position, product_id in enumerate(recent):
            if product_id in category_of:
                # Newer views count more than older ones.
                weights[category_of[product_id]] += 1.0 / (1.0 + 0.15 * position)
        for product_id in cart:
            if product_id in category_of:
                weights[category_of[product_id]] += 2.0
        for product_id in ordered:
            if product_id in category_of:
                weights[category_of[product_id]] += 1.5

    chosen = [cid for cid, _ in sorted(weights.items(), key=lambda kv: -kv[1])][:limit]
    for cid in _popular_categories(limit * 2):
        if len(chosen) >= limit:
            break
        if cid not in chosen:
            chosen.append(cid)
    return chosen


def category_shelves(request, shelves: int = 4, per_shelf: int = 12) -> list[dict]:
    """
    [{category, kind, title, product_ids}] for the shopper's top categories.

    Each category gets its best-seller shelf when it has enough sales to fill
    one, otherwise its trending shelf. Products already used by an earlier shelf
    are skipped, so two overlapping categories never repeat a tile.
    """
    from product.models import Category

    from .serving import _dismissed_ids

    category_ids = shopper_categories(request, limit=shelves)
    if not category_ids:
        return []

    names = {
        row['id']: row
        for row in Category.objects.filter(id__in=category_ids).values('id', 'title', 'slug')
    }
    hidden = _dismissed_ids(request)
    used: set[int] = set()
    result = []

    for category_id in category_ids:
        category = names.get(category_id)
        if not category:
            continue

        kind = KIND_BEST_SELLERS
        ids = category_ranking(category_id, KIND_BEST_SELLERS)
        if len(ids) < MIN_BEST_SELLERS:
            kind = KIND_TRENDING
            ids = category_ranking(category_id, KIND_TRENDING)

        picks = [pid for pid in ids if pid not in used and pid not in hidden][:per_shelf]
        if len(picks) < 2:
            continue
        used.update(picks)
        result.append({
            'category': category,
            'kind': kind,
            'title': f"{'Best sellers' if kind == KIND_BEST_SELLERS else 'Trending'} in {category['title']}",
            'product_ids': picks,
        })

    return result

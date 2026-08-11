"""
recommendation/serving.py

Request-time retrieval. Everything expensive already happened in the nightly run,
so these functions do little more than read an index and, where it helps, re-rank
a short list against live context.

Two principles run through the whole module.

**Never return an empty rail.** Every function degrades through a chain of
fallbacks — personalised → similar-item → category → trending → popular — and the
last link always produces something. A blank shelf on a storefront reads as
broken; a slightly generic shelf reads as a shop. This also means the code is
safe to deploy on day one, before a single model has been trained.

**Personalisation happens on a short list.** Fetching 60 precomputed candidates
and reordering them against what is in the cart right now is a few hundred
microseconds. Scoring the catalog per request is not, and the difference in
quality between the two is small.

Caches are versioned by model-run id, so a fresh model invalidates every cached
rail implicitly rather than relying on anyone remembering to flush.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db.models import Case, When

from .models import (
    KIND_CO_PURCHASE, KIND_CONTENT, KIND_HYBRID, ModelRun, NotInterested,
    ProductDealScore, ProductNeighbor, SURFACE_FOR_YOU, UserRecommendation,
)

logger = logging.getLogger(__name__)

CACHE_TTL = 600           # 10 minutes
DEAL_POOL = 120           # deals fetched before personalised re-ranking


def current_model_version() -> int:
    """
    Id of the newest completed training run — the cache-version key.

    Serving deliberately never reads a `running` run: a retrain in progress is
    invisible until it finishes, so a crash halfway through cannot put a
    half-written model in front of shoppers.
    """
    version = cache.get('rec:model_version')
    if version is None:
        run = (
            ModelRun.objects
            .filter(status=ModelRun.STATUS_COMPLETED)
            .order_by('-finished_at')
            .values_list('id', flat=True)
            .first()
        )
        version = run or 0
        cache.set('rec:model_version', version, 300)
    return version


def deals_version() -> int:
    """
    Counter bumped every time deals are rescored.

    Deal freshness moves on its own clock — hourly rescoring, plus a seller
    launching a flash sale at any moment — so the deals cache cannot hang off the
    model-run version the way the neighbour caches do. Bumping a counter
    invalidates every cached deal page at once without needing to know which
    keys exist.
    """
    return cache.get('rec:deals_version') or 0


def bump_deals_version() -> int:
    """Invalidate every cached deals page. Called after scoring and on price changes."""
    try:
        return cache.incr('rec:deals_version')
    except ValueError:
        # Key absent or expired — start the counter over. Worst case is one
        # extra cache miss.
        cache.set('rec:deals_version', 1, None)
        return 1


def visitor_key(request) -> str | None:
    """Reuse the identity scheme the existing view tracking already established."""
    try:
        from product.utils import _get_visitor_id
        return _get_visitor_id(request)
    except Exception:
        return None


def _ordered_by_ids(product_ids: list[int]):
    """Fetch published products preserving the given order in a single query."""
    from product.models import Product

    if not product_ids:
        return []
    ordering = Case(*[When(pk=pk, then=position) for position, pk in enumerate(product_ids)])
    return list(
        Product.published
        .filter(pk__in=product_ids)
        .select_related('vendor', 'sub_category')
        .order_by(ordering)
    )


def _dismissed_ids(request) -> set[int]:
    """Products the shopper explicitly asked not to see again."""
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return set(NotInterested.objects.filter(user=user).values_list('product_id', flat=True))
    key = visitor_key(request)
    if key:
        return set(NotInterested.objects.filter(visitor_key=key).values_list('product_id', flat=True))
    return set()


def _recent_product_ids(request, limit: int = 10) -> list[int]:
    """Recently viewed products — DB for signed-in shoppers, Redis for guests."""
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        from product.models import RecentlyViewedProduct
        return list(
            RecentlyViewedProduct.objects
            .filter(user=user).order_by('-viewed_at')
            .values_list('product_id', flat=True)[:limit]
        )
    try:
        from product.utils import get_recently_viewed_ids
        return get_recently_viewed_ids(request, limit)
    except Exception:
        return []


# ── Fallback chain ───────────────────────────────────────────────────────────

def _trending_ids(limit: int, exclude: set[int] | None = None) -> list[int]:
    from product.models import Product

    queryset = Product.published.order_by('-trending_score', '-views')
    if exclude:
        queryset = queryset.exclude(id__in=exclude)
    return list(queryset.values_list('id', flat=True)[:limit])


def _popular_ids(limit: int, exclude: set[int] | None = None) -> list[int]:
    from product.models import Product

    queryset = Product.published.order_by('-views', '-avg_rating')
    if exclude:
        queryset = queryset.exclude(id__in=exclude)
    return list(queryset.values_list('id', flat=True)[:limit])


def _fill(ids: list[int], limit: int, exclude: set[int]) -> list[int]:
    """Top a short rail up with trending, then popular, products."""
    if len(ids) >= limit:
        return ids[:limit]

    blocked = set(ids) | exclude
    for source in (_trending_ids, _popular_ids):
        if len(ids) >= limit:
            break
        for product_id in source(limit * 2, blocked):
            if product_id not in blocked:
                ids.append(product_id)
                blocked.add(product_id)
                if len(ids) >= limit:
                    break
    return ids[:limit]


# ── "You might also like this" ───────────────────────────────────────────────

def you_might_also_like(product_id: int, limit: int = 12, exclude: set[int] | None = None):
    """
    Substitutes for a product: the hybrid neighbour list, then content
    neighbours, then the same sub-category.

    Cached per product — a popular product's neighbour list is requested
    constantly and never changes between training runs.
    """
    exclude = set(exclude or ()) | {product_id}
    cache_key = f"rec:also_like:{current_model_version()}:{product_id}:{limit}"

    ids = cache.get(cache_key)
    if ids is None:
        ids = list(
            ProductNeighbor.objects
            .filter(product_id=product_id, kind=KIND_HYBRID)
            .order_by('rank')
            .values_list('neighbor_id', flat=True)[:limit * 2]
        )
        if len(ids) < limit:
            ids += list(
                ProductNeighbor.objects
                .filter(product_id=product_id, kind=KIND_CONTENT)
                .exclude(neighbor_id__in=ids)
                .order_by('rank')
                .values_list('neighbor_id', flat=True)[:limit * 2]
            )
        if len(ids) < limit:
            # No model yet, or a product too new to have been embedded.
            from product.models import Product
            sub_category_id = (
                Product.objects.filter(pk=product_id)
                .values_list('sub_category_id', flat=True).first()
            )
            if sub_category_id:
                ids += list(
                    Product.published
                    .filter(sub_category_id=sub_category_id)
                    .exclude(id__in=ids + [product_id])
                    .order_by('-trending_score')
                    .values_list('id', flat=True)[:limit * 2]
                )
        cache.set(cache_key, ids, CACHE_TTL)

    ids = [pid for pid in ids if pid not in exclude]
    return _ordered_by_ids(_fill(ids, limit, exclude))


# ── "Customers also bought" ──────────────────────────────────────────────────

def customers_also_bought(product_id: int, limit: int = 12, exclude: set[int] | None = None):
    """
    Complements rather than substitutes — items that appeared in the same orders.

    Falls back to the hybrid neighbours when a product has not yet been bought
    alongside anything, which is most of the catalog early on.
    """
    exclude = set(exclude or ()) | {product_id}
    cache_key = f"rec:also_bought:{current_model_version()}:{product_id}:{limit}"

    ids = cache.get(cache_key)
    if ids is None:
        ids = list(
            ProductNeighbor.objects
            .filter(product_id=product_id, kind=KIND_CO_PURCHASE)
            .order_by('rank')
            .values_list('neighbor_id', flat=True)[:limit * 2]
        )
        cache.set(cache_key, ids, CACHE_TTL)

    ids = [pid for pid in ids if pid not in exclude]
    if len(ids) < limit:
        return you_might_also_like(product_id, limit, exclude)
    return _ordered_by_ids(ids[:limit])


def more_from_seller(product_id: int, vendor_id: int | None = None, limit: int = 12):
    """
    Other products from the same seller, best first.

    Not a model output — a merchandising rail the marketplace needs. Sellers
    expect their catalog cross-linked from every listing they own, and it is
    often the shopper's cheapest route to a second item in the same shipment.
    """
    from product.models import Product

    if vendor_id is None:
        vendor_id = (
            Product.objects.filter(pk=product_id)
            .values_list('vendor_id', flat=True).first()
        )
    if not vendor_id:
        return []

    return list(
        Product.published
        .filter(vendor_id=vendor_id)
        .exclude(id=product_id)
        .select_related('vendor', 'sub_category')
        .order_by('-trending_score', '-avg_rating')[:limit]
    )


# ── Cart add-ons ─────────────────────────────────────────────────────────────

def cart_addons(cart_product_ids: list[int], limit: int = 10):
    """
    Suggestions for the whole basket, not one item at a time.

    Co-purchase scores are summed across everything in the cart, so a product
    that complements several items ranks above one that complements a single
    item strongly — which is the right answer for a "complete your order" rail.
    """
    if not cart_product_ids:
        return _ordered_by_ids(_trending_ids(limit))

    exclude = set(cart_product_ids)
    scores: dict[int, float] = {}

    rows = (
        ProductNeighbor.objects
        .filter(product_id__in=cart_product_ids, kind__in=(KIND_CO_PURCHASE, KIND_HYBRID))
        .values_list('neighbor_id', 'score', 'kind')
    )
    for neighbor_id, score, kind in rows:
        if neighbor_id in exclude:
            continue
        # Co-purchase evidence outranks embedding similarity for this rail:
        # complements are what completes a basket.
        weight = 1.0 if kind == KIND_CO_PURCHASE else 0.4
        scores[neighbor_id] = scores.get(neighbor_id, 0.0) + float(score) * weight

    ranked = [pid for pid, _ in sorted(scores.items(), key=lambda kv: -kv[1])]
    return _ordered_by_ids(_fill(ranked, limit, exclude))


# ── "Recommended for you" ────────────────────────────────────────────────────

def recommended_for_you(request, limit: int = 20):
    """
    The personalised rail.

    Signed-in shoppers read their precomputed list. Guests — most of the traffic
    at this stage — get one built live from the session's recently viewed
    products against the same neighbour table: a couple of indexed queries, and
    genuinely personal after a single product view, which is the moment it
    matters most.

    Returns (products, reasons) where reasons maps product id → explanation
    string, so the UI can caption each card.
    """
    user = getattr(request, 'user', None)
    dismissed = _dismissed_ids(request)

    if user is not None and user.is_authenticated:
        rows = list(
            UserRecommendation.objects
            .filter(user=user, surface=SURFACE_FOR_YOU)
            .exclude(product_id__in=dismissed)
            .order_by('rank')
            .values_list('product_id', 'reason_detail')[:limit]
        )
        if rows:
            ids = [product_id for product_id, _ in rows]
            reasons = {product_id: detail for product_id, detail in rows if detail}
            return _ordered_by_ids(ids), reasons

    # Session-based: expand the shopper's recent views through the neighbour table.
    recent = _recent_product_ids(request, limit=6)
    if recent:
        exclude = set(recent) | dismissed
        scores: dict[int, float] = {}
        seed_of: dict[int, int] = {}

        rows = (
            ProductNeighbor.objects
            .filter(product_id__in=recent, kind=KIND_HYBRID)
            .order_by('rank')
            .values_list('product_id', 'neighbor_id', 'score')
        )
        for seed_id, neighbor_id, score in rows:
            if neighbor_id in exclude:
                continue
            # Weight by recency: the most recently viewed product says the most
            # about what the shopper is looking for right now.
            recency = 1.0 / (1 + recent.index(seed_id)) if seed_id in recent else 0.5
            contribution = float(score) * recency
            if contribution > scores.get(neighbor_id, 0.0):
                seed_of[neighbor_id] = seed_id
            scores[neighbor_id] = scores.get(neighbor_id, 0.0) + contribution

        if scores:
            ranked = [pid for pid, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:limit]
            products = _ordered_by_ids(_fill(ranked, limit, exclude))

            from product.models import Product
            seed_titles = dict(
                Product.objects.filter(id__in=set(seed_of.values()))
                .values_list('id', 'title')
            )
            reasons = {
                product_id: f"Because you viewed {seed_titles[seed_id][:80]}"
                for product_id, seed_id in seed_of.items()
                if seed_id in seed_titles
            }
            return products, reasons

    # Nothing known about this visitor at all.
    ids = _fill([], limit, dismissed)
    return _ordered_by_ids(ids), {}


# ── "Today's Deals" ──────────────────────────────────────────────────────────

def todays_deals(request=None, limit: int = 20):
    """
    Deals ranked by learned quality, then nudged toward the shopper's interests.

    The base ordering is shared and cached — deal quality does not depend on who
    is asking. Personalisation is a light re-rank of that pool: a deal in a
    sub-category the shopper actually browses gets a boost, but a great deal
    stays near the top regardless. Deliberately gentle. Over-personalising a
    deals page hides the genuinely good offers from anyone whose history happens
    to point elsewhere, and those offers are the reason people open the page.
    """
    cache_key = f"rec:todays_deals:{deals_version()}:{DEAL_POOL}"
    pool = cache.get(cache_key)

    if pool is None:
        pool = list(
            ProductDealScore.objects
            .filter(is_eligible=True)
            .order_by('-score')
            .values_list('product_id', 'score')[:DEAL_POOL]
        )
        if not pool:
            # No scoring run yet — fall back to the plain discount ordering so
            # the page still works on a fresh deployment.
            from django.db.models import ExpressionWrapper, F, FloatField

            from product.models import Product
            fallback = (
                Product.published
                .filter(old_price__isnull=False, old_price__gt=F('price'))
                .annotate(discount=ExpressionWrapper(
                    (F('old_price') - F('price')) * 100.0 / F('old_price'),
                    output_field=FloatField(),
                ))
                .order_by('-discount')
                .values_list('id', 'discount')[:DEAL_POOL]
            )
            pool = list(fallback)
        cache.set(cache_key, pool, CACHE_TTL)

    if not pool:
        return _ordered_by_ids(_trending_ids(limit))

    dismissed = _dismissed_ids(request) if request is not None else set()
    pool = [(pid, score) for pid, score in pool if pid not in dismissed]

    affinity = _sub_category_affinity(request) if request is not None else {}
    if affinity:
        from product.models import Product

        sub_categories = dict(
            Product.objects.filter(id__in=[pid for pid, _ in pool])
            .values_list('id', 'sub_category_id')
        )
        max_score = max((score for _, score in pool), default=1.0) or 1.0
        pool = sorted(
            pool,
            key=lambda row: -(
                row[1] / max_score + 0.25 * affinity.get(sub_categories.get(row[0]), 0.0)
            ),
        )

    return _ordered_by_ids([pid for pid, _ in pool[:limit]])


def _sub_category_affinity(request) -> dict[int, float]:
    """
    Normalised 0–1 interest per sub-category, from the visitor's recent views.

    Cheap on purpose: two queries, no model artifacts, works for guests. It only
    has to be good enough to break ties on a deals page.
    """
    recent = _recent_product_ids(request, limit=15)
    if not recent:
        return {}

    from product.models import Product

    counts: dict[int, float] = {}
    rows = Product.objects.filter(id__in=recent).values_list('sub_category_id', flat=True)
    for sub_category_id in rows:
        if sub_category_id:
            counts[sub_category_id] = counts.get(sub_category_id, 0.0) + 1.0

    if not counts:
        return {}
    highest = max(counts.values())
    return {sub_category_id: value / highest for sub_category_id, value in counts.items()}


# ── "Keep shopping for" ──────────────────────────────────────────────────────

def keep_shopping(request, limit: int = 12):
    """
    The shopper's own recently viewed products, most recent first — the highest
    converting rail on most storefronts, and the cheapest to produce.
    """
    ids = _recent_product_ids(request, limit)
    return _ordered_by_ids(ids)

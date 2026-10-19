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
from .serializers import CARD_ONLY_FIELDS

logger = logging.getLogger(__name__)

CACHE_TTL = 600           # 10 minutes
DEAL_POOL = 300           # deals fetched before personalised re-ranking


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
        .only(*CARD_ONLY_FIELDS)
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
        .only(*CARD_ONLY_FIELDS)
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

#: Reciprocal-rank-fusion constant. Larger flattens the difference between
#: ranks; 10 keeps the top of each list clearly ahead of its tail.
RRF_K = 10

#: How much the live session counts against the nightly list. The session wins
#: once the shopper has done something the nightly run has not seen yet.
LIVE_WEIGHT_FRESH = 1.3
LIVE_WEIGHT_STALE = 0.6

#: Live candidates scoring below this share of the best live candidate are
#: dropped before fusion. Rank fusion rewards appearing in both lists, so a weak
#: tail match would otherwise lift every nightly pick over the session's real
#: interest.
LIVE_MIN_SHARE = 0.35


def _cart_product_ids(request) -> list[int]:
    """Products in the shopper's cart — the DB cart, or the guest cart header."""
    import json

    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        from order.models import Cart
        cart = Cart.objects.filter(user=user).first()
        if cart:
            return list(
                cart.cart_items.filter(product__isnull=False)
                .values_list('product_id', flat=True)
            )
        return []

    headers = getattr(request, 'headers', None)
    raw = headers.get('X-Guest-Cart') if headers is not None else None
    if not raw:
        return []
    try:
        items = json.loads(raw)
        return [int(item['p']) for item in items if item.get('p')]
    except (ValueError, TypeError, KeyError, AttributeError):
        return []


def _session_candidates(recent: list[int], cart: list[int], exclude: set[int]):
    """
    Live candidates from what the shopper is doing right now.

    Recent views expand through the substitute (hybrid) neighbours, newest view
    weighted most; cart items expand through both substitutes and complements,
    and count more — a cart add is a stronger statement than a glance.

    Returns ({product_id: score}, {product_id: seed_product_id}).
    """
    seeds: dict[int, float] = {}
    for position, product_id in enumerate(recent):
        seeds[product_id] = max(seeds.get(product_id, 0.0), 1.0 / (1 + position))
    for product_id in cart:
        seeds[product_id] = max(seeds.get(product_id, 0.0), 1.5)
    if not seeds:
        return {}, {}

    cart_set = set(cart)
    scores: dict[int, float] = {}
    seed_of: dict[int, int] = {}
    best_contribution: dict[int, float] = {}

    rows = (
        ProductNeighbor.objects
        .filter(product_id__in=list(seeds), kind__in=(KIND_HYBRID, KIND_CO_PURCHASE), rank__lt=20)
        .values_list('product_id', 'neighbor_id', 'score', 'kind')
    )
    for seed_id, neighbor_id, score, kind in rows:
        if neighbor_id in exclude:
            continue
        # Complements only make sense for what is in the basket.
        if kind == KIND_CO_PURCHASE and seed_id not in cart_set:
            continue
        contribution = float(score) * seeds.get(seed_id, 0.5)
        scores[neighbor_id] = scores.get(neighbor_id, 0.0) + contribution
        if contribution > best_contribution.get(neighbor_id, 0.0):
            best_contribution[neighbor_id] = contribution
            seed_of[neighbor_id] = seed_id
    return scores, seed_of


def _fuse(lists: list[tuple[list[int], float]]) -> dict[int, float]:
    """Weighted reciprocal-rank fusion: rank-based, so unrelated score scales mix safely."""
    fused: dict[int, float] = {}
    for ids, weight in lists:
        for rank, product_id in enumerate(ids):
            fused[product_id] = fused.get(product_id, 0.0) + weight / (RRF_K + rank + 1)
    return fused


def recommended_for_you(request, limit: int = 20):
    """
    The personalised rail — nightly model output fused with the live session.

    The nightly run knows the shopper's long-term taste; it does not know they
    spent this morning looking at rice cookers. So the precomputed list is
    blended, by reciprocal-rank fusion, with candidates expanded from their
    recent views and cart, and the session gets the larger say once it contains
    activity newer than the nightly list. Guests have no nightly list and get
    the session half alone.

    Products the shopper keeps scrolling past are pushed down; ones they hid,
    have just viewed, have in the cart, or bought since the last run are removed.

    Returns (products, reasons) where reasons maps product id → explanation
    string, so the UI can caption each card.
    """
    from . import engagement

    user = getattr(request, 'user', None)
    signed_in = user is not None and user.is_authenticated
    dismissed = _dismissed_ids(request)
    recent = _recent_product_ids(request, limit=8)
    cart = _cart_product_ids(request)
    exclude = dismissed | set(recent) | set(cart)

    precomputed: list[int] = []
    reasons: dict[int, str] = {}
    generated_at = None
    if signed_in:
        rows = list(
            UserRecommendation.objects
            .filter(user=user, surface=SURFACE_FOR_YOU)
            .order_by('rank')
            .values_list('product_id', 'reason_detail', 'generated_at')[:limit * 3]
        )
        if rows:
            generated_at = rows[0][2]
            from order.models import OrderProduct
            bought_since = set(
                OrderProduct.objects
                .filter(order__user=user, order__is_ordered=True, date_created__gte=generated_at)
                .values_list('product_id', flat=True)
            )
            for product_id, detail, _ in rows:
                if product_id in exclude or product_id in bought_since:
                    continue
                precomputed.append(product_id)
                if detail:
                    reasons[product_id] = detail

    live_scores, seed_of = _session_candidates(recent, cart, exclude)
    top_live = max(live_scores.values(), default=0.0)
    live = [
        pid for pid, score in sorted(live_scores.items(), key=lambda kv: -kv[1])
        if score >= LIVE_MIN_SHARE * top_live
    ][:limit * 3]

    if not precomputed and not live:
        return _ordered_by_ids(_fill([], limit, exclude)), {}

    fresh = bool(cart)
    if signed_in and generated_at is not None and not fresh:
        from product.models import RecentlyViewedProduct
        fresh = RecentlyViewedProduct.objects.filter(user=user, viewed_at__gt=generated_at).exists()
    live_weight = LIVE_WEIGHT_FRESH if (fresh or not precomputed) else LIVE_WEIGHT_STALE

    fused = _fuse([(precomputed, 1.0), (live, live_weight)])
    skipped = engagement.skipped_for_request(request)
    for product_id in skipped:
        if product_id in fused:
            fused[product_id] *= engagement.SKIP_PENALTY

    ranked = [pid for pid, _ in sorted(fused.items(), key=lambda kv: -kv[1])][:limit]

    # Caption live picks with the product that triggered them.
    seeds_needed = {seed_of[pid] for pid in ranked if pid in seed_of and pid not in reasons}
    if seeds_needed:
        from product.models import Product
        titles = dict(Product.objects.filter(id__in=seeds_needed).values_list('id', 'title'))
        cart_set = set(cart)
        for product_id in ranked:
            seed = seed_of.get(product_id)
            if product_id in reasons or seed not in titles:
                continue
            if seed in cart_set:
                reasons[product_id] = f"Goes with {titles[seed][:70]} in your cart"
            else:
                reasons[product_id] = f"Because you viewed {titles[seed][:70]}"

    return _ordered_by_ids(_fill(ranked, limit, exclude)), reasons


# ── "Today's Deals" ──────────────────────────────────────────────────────────

#: Diversity caps for the deals rail: a rail of six phone cases from one seller
#: is one deal shown six times.
DEAL_RAIL_MAX_PER_VENDOR = 2
DEAL_RAIL_MAX_PER_SUB_CATEGORY = 3


def deal_pool() -> list[tuple[int, float]]:
    """Eligible deals as (product_id, score), best first. Shared and cached."""
    cache_key = f"rec:todays_deals:{deals_version()}:{DEAL_POOL}"
    pool = cache.get(cache_key)
    if pool is not None:
        return pool

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
        pool = list(
            Product.published
            .filter(old_price__isnull=False, old_price__gt=F('price'))
            .annotate(discount=ExpressionWrapper(
                (F('old_price') - F('price')) * 100.0 / F('old_price'),
                output_field=FloatField(),
            ))
            .order_by('-discount')
            .values_list('id', 'discount')[:DEAL_POOL]
        )
    cache.set(cache_key, pool, CACHE_TTL)
    return pool


def personalize_deals(request, pool: list[tuple[int, float]], facts: dict[int, tuple]) -> list[int]:
    """
    Re-rank deals for one shopper: base deal quality, nudged by their category
    interests and by how well each deal has actually been clicked.

    `facts` maps product id → (vendor_id, sub_category_id, category_id).
    Deliberately gentle: over-personalising a deals page hides the genuinely
    good offers from anyone whose history points elsewhere, and those offers
    are the reason people open it.
    """
    from . import engagement

    if not pool:
        return []
    max_score = max((score for _, score in pool), default=1.0) or 1.0
    sub_affinity, cat_affinity = _category_affinity(request) if request is not None else ({}, {})
    lift = engagement_lift()
    skipped = engagement.skipped_for_request(request) if request is not None else set()

    def score(row):
        product_id, base = row
        _, sub_category, category = facts.get(product_id, (None, None, None))
        value = (
            base / max_score
            + 0.20 * sub_affinity.get(sub_category, 0.0)
            + 0.10 * cat_affinity.get(category, 0.0)
        )
        value *= lift.get(product_id, 1.0) ** 0.5
        if product_id in skipped:
            value *= engagement.SKIP_PENALTY
        return value

    return [product_id for product_id, _ in sorted(pool, key=lambda row: -score(row))]


def diversify(ids: list[int], facts: dict[int, tuple], limit: int,
              max_per_vendor: int, max_per_sub_category: int) -> list[int]:
    """Cap sellers and sub-categories in the visible slice; overflow goes after."""
    picked, overflow = [], []
    per_vendor: dict = {}
    per_sub: dict = {}
    for product_id in ids:
        vendor, sub_category, _ = facts.get(product_id, (None, None, None))
        if (per_vendor.get(vendor, 0) >= max_per_vendor
                or (sub_category and per_sub.get(sub_category, 0) >= max_per_sub_category)):
            overflow.append(product_id)
            continue
        per_vendor[vendor] = per_vendor.get(vendor, 0) + 1
        if sub_category:
            per_sub[sub_category] = per_sub.get(sub_category, 0) + 1
        picked.append(product_id)
        if len(picked) >= limit:
            return picked
    return (picked + overflow)[:limit]


def _deal_facts(product_ids: list[int]) -> dict[int, tuple]:
    from product.models import Product

    return {
        pid: (vendor, sub_category, category)
        for pid, vendor, sub_category, category in (
            Product.objects.filter(id__in=product_ids)
            .values_list('id', 'vendor_id', 'sub_category_id', 'sub_category__category_id')
        )
    }


def todays_deals(request=None, limit: int = 20):
    """
    The deals rail: learned deal quality, gently personalised, then capped so
    no seller or sub-category takes over the rail.
    """
    pool = deal_pool()
    if not pool:
        return _ordered_by_ids(_trending_ids(limit))

    dismissed = _dismissed_ids(request) if request is not None else set()
    pool = [(pid, score) for pid, score in pool if pid not in dismissed]

    facts = _deal_facts([pid for pid, _ in pool])
    ranked = personalize_deals(request, pool, facts)
    ranked = diversify(
        ranked, facts, limit,
        max_per_vendor=DEAL_RAIL_MAX_PER_VENDOR,
        max_per_sub_category=DEAL_RAIL_MAX_PER_SUB_CATEGORY,
    )
    return _ordered_by_ids(ranked)


def engagement_lift() -> dict[int, float]:
    """Product click lift (see engagement.py), cached for the life of a model run."""
    cache_key = f"rec:lift:{current_model_version()}"
    lift = cache.get(cache_key)
    if lift is None:
        from . import engagement
        lift = engagement.product_lift()
        cache.set(cache_key, lift, 3600)
    return lift


def _category_affinity(request) -> tuple[dict[int, float], dict[int, float]]:
    """
    Normalised 0–1 interest per sub-category and per category, from the
    visitor's recent views and cart.

    Cheap on purpose: two queries, no model artifacts, works for guests.
    """
    recent = _recent_product_ids(request, limit=15)
    cart = _cart_product_ids(request)
    if not recent and not cart:
        return {}, {}

    from product.models import Product

    weight = {pid: 1.0 for pid in recent}
    for pid in cart:
        weight[pid] = weight.get(pid, 0.0) + 2.0

    subs: dict[int, float] = {}
    cats: dict[int, float] = {}
    rows = (
        Product.objects.filter(id__in=list(weight))
        .values_list('id', 'sub_category_id', 'sub_category__category_id')
    )
    for pid, sub_category, category in rows:
        if sub_category:
            subs[sub_category] = subs.get(sub_category, 0.0) + weight[pid]
        if category:
            cats[category] = cats.get(category, 0.0) + weight[pid]

    def scaled(counts):
        if not counts:
            return {}
        highest = max(counts.values())
        return {key: value / highest for key, value in counts.items()}

    return scaled(subs), scaled(cats)


def _sub_category_affinity(request) -> dict[int, float]:
    """Sub-category half of `_category_affinity`, kept for existing callers."""
    return _category_affinity(request)[0]


# ── "Keep shopping for" ──────────────────────────────────────────────────────

def keep_shopping(request, limit: int = 12):
    """
    The shopper's own recently viewed products, most recent first — the highest
    converting rail on most storefronts, and the cheapest to produce.
    """
    ids = _recent_product_ids(request, limit)
    return _ordered_by_ids(ids)

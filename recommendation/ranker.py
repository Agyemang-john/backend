"""
recommendation/ranker.py

Builds the "Recommended for you" rail for every known shopper.

Two stages, which is how every large recommender is built and for a good reason.

**Candidate generation** casts a wide, cheap net — a few hundred plausible
products per shopper, drawn from four independent sources so that no single
model's blind spot becomes the shopper's experience:

  1. the ALS model's own top predictions (behavioural, needs data)
  2. neighbours of what they recently viewed, saved or bought (works from one
     interaction, and produces the most convincing explanations)
  3. their revealed category and brand affinities (broad, survives sparse data)
  4. live deals and trending products (guarantees a full rail no matter what)

**Ranking** then scores that small pool properly, blending collaborative
affinity, similarity strength, deal quality, product quality, trend and
price-fit — signals that pull in different directions and would each fail alone.

Two things happen after scoring that matter as much as the scoring itself:

* **Filters.** Sold out, unpublished, paused seller, already bought, explicitly
  dismissed — all removed. A recommendation the shopper cannot act on is worse
  than an empty slot.

* **Diversity.** A raw score ordering returns ten near-identical products,
  because whatever the model liked about the first it likes about its nine
  nearest neighbours. Caps per sub-category and per seller cost a little
  predicted relevance and buy back the variety that makes a rail worth scrolling.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import numpy as np
from django.utils import timezone

from .als import recommend_for_user
from .models import (
    REASON_BRAND_AFFINITY, REASON_CATEGORY_AFFINITY, REASON_COLLABORATIVE, REASON_DEAL,
    REASON_POPULAR, REASON_SIMILAR_TO_VIEWED, REASON_TRENDING, SURFACE_FOR_YOU,
    NotInterested, UserRecommendation,
)

logger = logging.getLogger(__name__)


# ── Blend weights ────────────────────────────────────────────────────────────
W_CF         = 0.35      # scaled by cf_trust — near zero until the data earns it
W_SIMILARITY = 0.30
W_DEAL       = 0.12
W_QUALITY    = 0.10
W_TREND      = 0.08
W_PRICE_FIT  = 0.05

#: Recent interactions used as explanation seeds.
MAX_SEEDS = 8

#: Candidates scored per shopper. Wide enough that filters and diversity caps
#: cannot starve the rail, small enough to stay cheap.
CANDIDATE_POOL = 400

#: Stored recommendations per shopper — several screens of infinite scroll.
TOP_N = 40

#: Diversity caps within the final rail.
MAX_PER_SUB_CATEGORY = 3
MAX_PER_VENDOR = 4


def _normalize(values: dict[int, float]) -> dict[int, float]:
    """Min-max a score dict into 0–1 so unrelated signals can be added together."""
    if not values:
        return {}
    array = np.fromiter(values.values(), dtype=np.float64, count=len(values))
    low, high = float(array.min()), float(array.max())
    if high <= low:
        return {key: 1.0 for key in values}
    span = high - low
    return {key: (value - low) / span for key, value in values.items()}


class ProductCatalog:
    """
    Product metadata as parallel numpy arrays, indexed by embedding position.

    Loaded once per training run. Per-shopper scoring then reads attributes by
    array index instead of touching the database — with thousands of shoppers
    each scoring hundreds of candidates, an ORM lookup in that loop is the
    difference between seconds and hours.
    """

    def __init__(self, product_ids: list[int]):
        from product.models import Product

        self.product_ids = product_ids
        self.position = {pid: i for i, pid in enumerate(product_ids)}
        size = len(product_ids)

        self.sub_category = np.zeros(size, dtype=np.int64)
        self.vendor = np.zeros(size, dtype=np.int64)
        self.brand = np.zeros(size, dtype=np.int64)
        self.price = np.zeros(size, dtype=np.float64)
        self.rating = np.zeros(size, dtype=np.float64)
        self.review_count = np.zeros(size, dtype=np.float64)
        self.trending = np.zeros(size, dtype=np.float64)
        self.title = [''] * size

        rows = (
            Product.published
            .filter(id__in=product_ids)
            .values_list(
                'id', 'sub_category_id', 'vendor_id', 'brand_id',
                'price', 'avg_rating', 'review_count', 'trending_score', 'title',
            )
        )
        self.sellable: set[int] = set()
        for pid, sub_cat, vendor, brand, price, rating, reviews, trending, title in rows:
            index = self.position.get(pid)
            if index is None:
                continue
            self.sellable.add(index)
            self.sub_category[index] = sub_cat or 0
            self.vendor[index] = vendor or 0
            self.brand[index] = brand or 0
            self.price[index] = float(price or 0)
            self.rating[index] = float(rating or 0)
            self.review_count[index] = float(reviews or 0)
            self.trending[index] = float(trending or 0)
            self.title[index] = title or ''

        self.deal_score = np.zeros(size, dtype=np.float64)
        self.in_stock = np.ones(size, dtype=bool)
        self._load_deal_state()

    def _load_deal_state(self):
        from .models import ProductDealScore

        rows = ProductDealScore.objects.values_list('product_id', 'score', 'is_eligible', 'stock_remaining')
        for pid, score, eligible, stock in rows:
            index = self.position.get(pid)
            if index is None:
                continue
            if eligible:
                self.deal_score[index] = float(score or 0)
            self.in_stock[index] = (stock or 0) > 0

    def is_recommendable(self, index: int) -> bool:
        return index in self.sellable and bool(self.in_stock[index])


def _history(dataset, row: int, to_catalog: dict[int, int]) -> list[tuple[int, float]]:
    """
    One shopper's history as (catalog index, weight) pairs.

    The translation is the important part. The dataset matrix is indexed by the
    *pruned* set of products that had enough interactions to model, while
    embeddings, neighbours and product metadata are indexed by the full published
    catalog. The two orderings are unrelated. Reading a dataset column as though
    it were a catalog position silently points at a different product, which is
    the kind of bug that produces recommendations that look plausible and are
    entirely wrong.
    """
    matrix = dataset.matrix
    start, end = matrix.indptr[row], matrix.indptr[row + 1]
    if start == end:
        return []

    pairs = []
    for column, weight in zip(matrix.indices[start:end], matrix.data[start:end]):
        index = to_catalog.get(int(column))
        if index is not None:
            pairs.append((index, float(weight)))
    return pairs


def _seed_interactions(history: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """
    The shopper's strongest recent interactions, used both as similarity seeds and
    as the source of explanations.

    Decayed weight already folds recency and intent together, so the heaviest
    entries are the recent, high-intent ones — precisely what "Because you
    viewed…" should point at.
    """
    return sorted(history, key=lambda pair: -pair[1])[:MAX_SEEDS]


def _affinity_profile(history: list[tuple[int, float]], catalog: ProductCatalog):
    """
    Revealed taste: which sub-categories and brands this shopper's history
    concentrates in, weighted by interaction strength.

    Crude next to a learned embedding, and considerably more robust when a
    shopper has six interactions rather than six hundred — which describes almost
    everyone at this stage.
    """
    sub_categories: dict[int, float] = defaultdict(float)
    brands: dict[int, float] = defaultdict(float)
    prices: list[float] = []

    for index, weight in history:
        if catalog.sub_category[index]:
            sub_categories[int(catalog.sub_category[index])] += weight
        if catalog.brand[index]:
            brands[int(catalog.brand[index])] += weight
        if catalog.price[index] > 0:
            prices.append(float(catalog.price[index]))

    typical_price = float(np.median(prices)) if prices else 0.0
    return sub_categories, brands, typical_price


def _price_fit(price: float, typical_price: float) -> float:
    """
    How close a product sits to the shopper's usual spend, 0–1.

    Someone who browses ₵50 accessories should not have the rail filled with
    ₵5,000 televisions merely because the model finds them broadly appealing.
    Log-ratio distance, so it treats "half the price" and "twice the price" as
    equally far away.
    """
    if typical_price <= 0 or price <= 0:
        return 0.5
    ratio = np.log(price / typical_price)
    return float(np.exp(-(ratio ** 2) / 2.0))


def _generate_candidates(
    history, user_vector, item_factors, neighbors, catalog, seen, cf_trust,
):
    """
    Four independent sources, each tagged with why it fired.

    Every index here — `history`, `seen`, `neighbors`, `item_factors`, the keys of
    the returned dict — is a catalog position. Translation from dataset columns
    happens once, in the caller.

    Returns {candidate_index: {source: score}} — a product surfacing from several
    sources at once is a stronger signal than one that squeaked in from a single
    place, and the merged scoring below rewards that.
    """
    candidates: dict[int, dict[str, float]] = defaultdict(dict)

    # 1. Collaborative — only when the dataset supports it at all.
    if cf_trust > 0 and user_vector is not None and item_factors.size:
        for index, score in recommend_for_user(user_vector, item_factors, n=150, exclude=seen):
            candidates[index]['cf'] = score

    # 2. Similar to what they engaged with — the explainable core of the rail.
    for seed_index, seed_weight in _seed_interactions(history):
        for neighbor_index, similarity in neighbors.get(seed_index, [])[:20]:
            if neighbor_index in seen:
                continue
            contribution = similarity * np.log1p(seed_weight)
            existing = candidates[neighbor_index].get('similarity', 0.0)
            if contribution > existing:
                candidates[neighbor_index]['similarity'] = contribution
                candidates[neighbor_index]['seed'] = seed_index

    # 3. Category and brand affinity — broad, and resilient to sparse history.
    sub_categories, brands, typical_price = _affinity_profile(history, catalog)
    if sub_categories or brands:
        top_sub_categories = {
            sub_cat for sub_cat, _ in sorted(sub_categories.items(), key=lambda kv: -kv[1])[:5]
        }
        top_brands = {brand for brand, _ in sorted(brands.items(), key=lambda kv: -kv[1])[:5]}

        for index in range(len(catalog.product_ids)):
            if index in seen or not catalog.is_recommendable(index):
                continue
            in_category = int(catalog.sub_category[index]) in top_sub_categories
            in_brand = int(catalog.brand[index]) in top_brands
            if not (in_category or in_brand):
                continue
            # Rank within the affinity group by quality, not arbitrarily.
            base = 0.5 * (catalog.rating[index] / 5.0) + 0.5 * min(catalog.trending[index] / 100.0, 1.0)
            if in_category:
                candidates[index]['category'] = base
            if in_brand:
                candidates[index]['brand'] = base

    # 4. Deals and trending — the floor that guarantees a full, useful rail.
    for index in np.argsort(-catalog.deal_score)[:60]:
        index = int(index)
        if index not in seen and catalog.deal_score[index] > 0 and catalog.is_recommendable(index):
            candidates[index]['deal'] = float(catalog.deal_score[index])

    for index in np.argsort(-catalog.trending)[:60]:
        index = int(index)
        if index not in seen and catalog.is_recommendable(index):
            candidates[index]['trending'] = float(catalog.trending[index])

    return candidates, typical_price


def _pick_reason(sources: dict[str, float], normalized: dict[str, dict[int, float]], index: int, catalog):
    """
    Attribute the recommendation to whichever source contributed most of its
    score, and turn that into a sentence.

    The explanation is not decoration. It tells the shopper why they are seeing
    something, and it tells whoever debugs a bad recommendation where to look.
    """
    contributions = {
        'cf':         W_CF * normalized['cf'].get(index, 0.0),
        'similarity': W_SIMILARITY * normalized['similarity'].get(index, 0.0),
        'deal':       W_DEAL * normalized['deal'].get(index, 0.0),
        'category':   W_QUALITY * normalized['category'].get(index, 0.0),
        'brand':      W_QUALITY * normalized['brand'].get(index, 0.0),
        'trending':   W_TREND * normalized['trending'].get(index, 0.0),
    }
    contributions = {key: value for key, value in contributions.items() if key in sources}
    if not contributions:
        return REASON_POPULAR, ''

    dominant = max(contributions, key=contributions.get)

    if dominant == 'similarity' and 'seed' in sources:
        seed_index = int(sources['seed'])
        seed_title = catalog.title[seed_index] if seed_index < len(catalog.title) else ''
        if seed_title:
            return REASON_SIMILAR_TO_VIEWED, f"Because you viewed {seed_title[:80]}"
        return REASON_SIMILAR_TO_VIEWED, 'Similar to items you viewed'
    if dominant == 'cf':
        return REASON_COLLABORATIVE, 'Shoppers with similar taste also bought this'
    if dominant == 'deal':
        return REASON_DEAL, 'On deal right now'
    if dominant == 'category':
        return REASON_CATEGORY_AFFINITY, 'From a category you shop'
    if dominant == 'brand':
        return REASON_BRAND_AFFINITY, 'From a brand you like'
    if dominant == 'trending':
        return REASON_TRENDING, 'Trending on Negromart'
    return REASON_POPULAR, 'Popular right now'


def _apply_diversity(ranked: list[tuple[int, float, str, str]], catalog: ProductCatalog, limit: int):
    """
    Enforce per-sub-category and per-seller caps while preserving score order.

    Overflow is not discarded — it is appended after the diverse selection, so a
    thin catalog still produces a full rail instead of four items and whitespace.
    """
    selected: list[tuple[int, float, str, str]] = []
    overflow: list[tuple[int, float, str, str]] = []
    per_sub_category: dict[int, int] = defaultdict(int)
    per_vendor: dict[int, int] = defaultdict(int)

    for entry in ranked:
        index = entry[0]
        sub_category = int(catalog.sub_category[index])
        vendor = int(catalog.vendor[index])

        if (per_sub_category[sub_category] >= MAX_PER_SUB_CATEGORY
                or per_vendor[vendor] >= MAX_PER_VENDOR):
            overflow.append(entry)
            continue

        per_sub_category[sub_category] += 1
        per_vendor[vendor] += 1
        selected.append(entry)
        if len(selected) >= limit:
            return selected

    return (selected + overflow)[:limit]


def build_user_recommendations(
    dataset,
    user_factors: np.ndarray,
    item_factors: np.ndarray,
    neighbors: dict[int, list[tuple[int, float]]],
    product_ids: list[int],
    model_run,
    cf_trust: float,
    top_n: int = TOP_N,
) -> int:
    """
    Score and persist the "Recommended for you" rail for every authenticated
    shopper in the dataset. Returns the number of rows written.

    Guests are deliberately skipped here — there is nowhere durable to store a
    result for them. serving.py builds their rail live from the session's recently
    viewed products against the same neighbour table, which costs one query.
    """
    from django.db import transaction

    if not dataset.auth_user_ids:
        logger.info("ranker: no authenticated shoppers in the dataset")
        return 0

    catalog = ProductCatalog(product_ids)

    # Dataset column → catalog position. The dataset is built from the pruned set
    # of products with enough interactions to model; everything else in the
    # pipeline is indexed by the full published catalog. Nothing may cross
    # between the two without going through this map.
    to_catalog = {
        column: catalog.position[product_id]
        for product_id, column in dataset.item_pos.items()
        if product_id in catalog.position
    }

    dismissed: dict[int, set[int]] = defaultdict(set)
    for user_id, product_id in NotInterested.objects.values_list('user_id', 'product_id'):
        if user_id:
            index = catalog.position.get(product_id)
            if index is not None:
                dismissed[user_id].add(index)

    generated_at = timezone.now()
    rows: list[UserRecommendation] = []

    for row, user_id in dataset.auth_user_ids.items():
        history = _history(dataset, row, to_catalog)
        seen = {index for index, _ in history} | dismissed.get(user_id, set())

        user_vector = user_factors[row] if row < user_factors.shape[0] else None
        candidates, typical_price = _generate_candidates(
            history, user_vector, item_factors, neighbors, catalog, seen, cf_trust,
        )
        if not candidates:
            continue

        # Cap the pool before the expensive per-candidate work.
        if len(candidates) > CANDIDATE_POOL:
            best = sorted(
                candidates.items(),
                key=lambda kv: -max(v for k, v in kv[1].items() if k != 'seed'),
            )[:CANDIDATE_POOL]
            candidates = dict(best)

        # Normalise each source independently — an ALS dot product and a deal
        # score out of 100 are not comparable until they are both 0–1.
        normalized = {
            source: _normalize({
                index: sources[source]
                for index, sources in candidates.items() if source in sources
            })
            for source in ('cf', 'similarity', 'deal', 'category', 'brand', 'trending')
        }

        ranked: list[tuple[int, float, str, str]] = []
        for index, sources in candidates.items():
            # Each generator already skips `seen`, but recommending someone a
            # product they just bought is the most visible failure this rail has,
            # so it is checked once more where it cannot be missed.
            if index in seen or not catalog.is_recommendable(index):
                continue

            score = (
                W_CF * cf_trust * normalized['cf'].get(index, 0.0)
                + W_SIMILARITY * normalized['similarity'].get(index, 0.0)
                + W_DEAL * normalized['deal'].get(index, 0.0)
                + W_TREND * normalized['trending'].get(index, 0.0)
                + W_QUALITY * max(
                    normalized['category'].get(index, 0.0),
                    normalized['brand'].get(index, 0.0),
                    catalog.rating[index] / 5.0,
                )
                + W_PRICE_FIT * _price_fit(float(catalog.price[index]), typical_price)
            )
            reason, detail = _pick_reason(sources, normalized, index, catalog)
            ranked.append((index, score, reason, detail))

        if not ranked:
            continue

        ranked.sort(key=lambda entry: -entry[1])
        final = _apply_diversity(ranked, catalog, top_n)

        for rank, (index, score, reason, detail) in enumerate(final):
            seed = candidates[index].get('seed')
            rows.append(UserRecommendation(
                user_id=user_id,
                product_id=product_ids[index],
                surface=SURFACE_FOR_YOU,
                score=round(float(score), 6),
                rank=rank,
                reason=reason,
                reason_detail=detail[:200],
                source_product_id=product_ids[int(seed)] if seed is not None else None,
                model_run=model_run,
                generated_at=generated_at,
            ))

    with transaction.atomic():
        UserRecommendation.objects.filter(surface=SURFACE_FOR_YOU).delete()
        UserRecommendation.objects.bulk_create(rows, batch_size=5000, ignore_conflicts=True)

    logger.info(
        "ranker: wrote %d recommendations for %d shoppers", len(rows), len(dataset.auth_user_ids),
    )
    return len(rows)

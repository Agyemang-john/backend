"""
recommendation/similarity.py

Turns the two embedding spaces into the item→item neighbour lists the storefront
actually serves, and computes co-purchase pairs separately.

The blend is the interesting part. Collaborative similarity is better *when it
has evidence* — it captures that shoppers who buy this charger buy that cable,
which no amount of text analysis would reveal. Content similarity is always
available but literal. So neither is used alone:

    similarity(i, j) = a·cosine_cf(i, j) + (1 − a)·cosine_content(i, j)

where `a` is decided **per pair**, from how many distinct shoppers have actually
touched each of the two products (and how dense the dataset is overall). A pair
of well-trodden products leans on behaviour; a pair involving something listed
yesterday leans on content. The storefront therefore shifts from content-driven
to behaviour-driven on its own as the marketplace grows, without a flag anyone
has to remember to flip.

Substitutes vs. complements is a separate axis, so co-purchase is kept as its own
neighbour kind. "You might also like this" wants substitutes — another running
shoe. "Customers also bought" and cart add-ons want complements — the socks. Both
computed here; the serving layer picks the right one for each rail.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import numpy as np

from .als import top_k_neighbors
from .models import KIND_CO_PURCHASE, KIND_CONTENT, KIND_HYBRID, ProductNeighbor

logger = logging.getLogger(__name__)

#: Distinct shoppers before an item's collaborative vector is fully trusted.
CF_ITEM_CONFIDENCE = 8.0

#: Neighbours kept per product per kind.
TOP_K = 30

#: Below this cosine a "neighbour" is noise dressed up as a recommendation.
MIN_SIMILARITY = 0.05

#: Orders containing more than this are wholesale or test data, not a shopping
#: basket — including them would make every product co-purchased with everything.
MAX_BASKET_SIZE = 25

#: Distinct orders required before a co-purchase pair is trusted.
MIN_CO_PURCHASE_SUPPORT = 2


def _alpha_per_item(item_users: np.ndarray, cf_trust: float) -> np.ndarray:
    """
    Per-item weight on collaborative similarity, 0–1.

    Two gates, multiplied: how much evidence this specific item has, and how much
    the dataset as a whole justifies trusting collaborative filtering at all. An
    item with plenty of traffic still gets little CF weight in a marketplace
    that has barely any data yet — which is the correct, humble answer.
    """
    if item_users.size == 0 or cf_trust <= 0:
        return np.zeros(item_users.shape[0] if item_users.size else 0, dtype=np.float32)
    per_item = np.minimum(1.0, item_users / CF_ITEM_CONFIDENCE)
    return (per_item * cf_trust).astype(np.float32)


def blend_neighbors(
    product_ids: list[int],
    cf_vectors: np.ndarray,
    content_vectors: np.ndarray,
    item_users: np.ndarray,
    cf_trust: float,
    top_k: int = TOP_K,
    block_size: int = 256,
) -> dict[int, list[tuple[int, float]]]:
    """
    Blended top-K neighbours for every product.

    All three inputs are row-aligned to `product_ids`. Products with no
    collaborative vector must have a zero row in `cf_vectors` — their alpha is
    then zero and they fall through to content similarity, which is exactly the
    cold-start behaviour wanted.

    Similarity is computed in row blocks: the full N×N matrix is never
    materialised, so memory stays flat as the catalog grows.
    """
    n_items = len(product_ids)
    if n_items < 2:
        return {}

    alpha = _alpha_per_item(item_users, cf_trust)
    if alpha.shape[0] != n_items:
        alpha = np.zeros(n_items, dtype=np.float32)

    has_cf = np.linalg.norm(cf_vectors, axis=1) > 0 if cf_vectors.size else np.zeros(n_items, bool)
    alpha = alpha * has_cf.astype(np.float32)

    top_k = min(top_k, n_items - 1)
    neighbors: dict[int, list[tuple[int, float]]] = {}

    for start in range(0, n_items, block_size):
        end = min(start + block_size, n_items)

        sim_content = content_vectors[start:end] @ content_vectors.T
        if cf_vectors.size:
            sim_cf = cf_vectors[start:end] @ cf_vectors.T
        else:
            sim_cf = np.zeros_like(sim_content)

        # A pair is only as trustworthy as its less-established half.
        pair_alpha = np.minimum(alpha[start:end][:, None], alpha[None, :])
        blended = pair_alpha * sim_cf + (1.0 - pair_alpha) * sim_content

        for local, absolute in enumerate(range(start, end)):
            blended[local, absolute] = -np.inf      # never its own neighbour

        for local, absolute in enumerate(range(start, end)):
            scores = blended[local]
            picks = np.argpartition(-scores, top_k - 1)[:top_k]
            picks = picks[np.argsort(-scores[picks])]
            selected = [
                (int(j), float(scores[j]))
                for j in picks
                if np.isfinite(scores[j]) and scores[j] >= MIN_SIMILARITY
            ]
            if selected:
                neighbors[absolute] = selected

    logger.info("similarity: blended neighbours for %d products (cf_trust=%.2f)", len(neighbors), cf_trust)
    return neighbors


def build_co_purchase(lookback_days: int = 365, top_k: int = TOP_K) -> dict[int, list[tuple[int, float, int]]]:
    """
    Products bought in the same order, scored by cosine-normalised co-occurrence:

        score(i, j) = co(i, j) / sqrt(count(i) · count(j))

    Normalisation is what stops the platform's best-seller from appearing as a
    "complement" to every product in the catalog. Raw co-occurrence counts always
    peak on popular items; dividing by each product's own frequency asks the
    sharper question — of the people who bought i, what fraction unusually often
    also bought j?

    Returns {product_id: [(neighbour_id, score, support), ...]}.
    """
    from datetime import timedelta

    from django.utils import timezone

    from order.models import OrderProduct

    since = timezone.now() - timedelta(days=lookback_days)

    baskets: dict[int, set[int]] = defaultdict(set)
    rows = (
        OrderProduct.objects
        .filter(date_created__gte=since, order__is_ordered=True, product__isnull=False)
        .values_list('order_id', 'product_id')
        .iterator(chunk_size=5000)
    )
    for order_id, product_id in rows:
        baskets[order_id].add(product_id)

    item_counts: dict[int, int] = defaultdict(int)
    pair_counts: dict[tuple[int, int], int] = defaultdict(int)

    usable = 0
    for products in baskets.values():
        if len(products) < 2 or len(products) > MAX_BASKET_SIZE:
            # Single-item orders say nothing about pairings; oversized ones say
            # everything about everything, which is the same as saying nothing.
            for product_id in products:
                item_counts[product_id] += 1
            continue

        usable += 1
        ordered = sorted(products)
        for product_id in ordered:
            item_counts[product_id] += 1
        for a_idx, a in enumerate(ordered):
            for b in ordered[a_idx + 1:]:
                pair_counts[(a, b)] += 1

    if not pair_counts:
        logger.info("similarity: no multi-item orders yet — co-purchase model is empty")
        return {}

    scored: dict[int, list[tuple[int, float, int]]] = defaultdict(list)
    for (a, b), support in pair_counts.items():
        if support < MIN_CO_PURCHASE_SUPPORT:
            continue
        denominator = np.sqrt(item_counts[a] * item_counts[b])
        if denominator <= 0:
            continue
        score = float(support / denominator)
        scored[a].append((b, score, support))
        scored[b].append((a, score, support))

    trimmed = {
        product_id: sorted(pairs, key=lambda row: (-row[1], -row[2]))[:top_k]
        for product_id, pairs in scored.items()
    }

    logger.info(
        "similarity: co-purchase from %d usable baskets → %d products with neighbours",
        usable, len(trimmed),
    )
    return trimmed


def content_only_neighbors(
    content_vectors: np.ndarray,
    top_k: int = TOP_K,
) -> dict[int, list[tuple[int, float]]]:
    """Pure content neighbours — the guaranteed fallback for any product."""
    return top_k_neighbors(content_vectors, k=top_k, min_score=MIN_SIMILARITY)


def persist_neighbors(
    product_ids: list[int],
    hybrid: dict[int, list[tuple[int, float]]],
    content: dict[int, list[tuple[int, float]]],
    co_purchase: dict[int, list[tuple[int, float, int]]],
    item_users: np.ndarray,
    model_run,
    batch_size: int = 5000,
) -> int:
    """
    Replace the neighbour table for this run.

    Delete-then-insert rather than upsert: a full rebuild is the only way to
    retire pairings the new model no longer believes in, and at O(products × K)
    rows it is fast. Done inside one transaction so the serving path never
    observes a half-built table.
    """
    from django.db import transaction

    rows: list[ProductNeighbor] = []

    def _emit(source_id: int, target_id: int, kind: str, score: float, rank: int, support: int):
        if source_id == target_id:
            return
        rows.append(ProductNeighbor(
            product_id=source_id, neighbor_id=target_id, kind=kind,
            score=round(float(score), 6), rank=rank, support=int(support),
            model_run=model_run,
        ))

    for index, picks in hybrid.items():
        source_id = product_ids[index]
        for rank, (neighbor_index, score) in enumerate(picks):
            support = int(item_users[neighbor_index]) if item_users.size > neighbor_index else 0
            _emit(source_id, product_ids[neighbor_index], KIND_HYBRID, score, rank, support)

    for index, picks in content.items():
        source_id = product_ids[index]
        for rank, (neighbor_index, score) in enumerate(picks):
            _emit(source_id, product_ids[neighbor_index], KIND_CONTENT, score, rank, 0)

    sellable = set(product_ids)
    for source_id, picks in co_purchase.items():
        if source_id not in sellable:
            continue
        rank = 0
        for neighbor_id, score, support in picks:
            if neighbor_id not in sellable:
                continue
            _emit(source_id, neighbor_id, KIND_CO_PURCHASE, score, rank, support)
            rank += 1

    with transaction.atomic():
        ProductNeighbor.objects.all().delete()
        ProductNeighbor.objects.bulk_create(rows, batch_size=batch_size, ignore_conflicts=True)

    logger.info("similarity: persisted %d neighbour rows", len(rows))
    return len(rows)

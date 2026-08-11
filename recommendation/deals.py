"""
recommendation/deals.py

The scoring model behind "Today's Deals".

The endpoint this replaces ranked deals by `ORDER BY discount_pct DESC`, which
optimises for exactly the wrong thing: the top of the page goes to whichever
seller typed the largest number into `old_price`, on a product nobody wants, that
may be out of stock. A shopper learns within a week that the deals page is not
worth opening.

This scores a deal the way a merchandiser would, across five components:

    score = 0.40·discount + 0.25·demand + 0.20·quality
          + 0.10·scarcity + 0.05·freshness

and every component is stored alongside the total, so a ranking can be explained
and retuned rather than argued about.

The component that does the most work is **discount credibility**. A claimed
discount is checked against the product's own 30-day price history: if the price
did not actually fall, the discount is discounted. That single check is what
separates a deals page from a page of inflated `old_price` fields, and it is why
ProductPriceHistory exists.

All signals are converted to percentile ranks within the current candidate pool
before weighting. Absolute thresholds do not survive contact with a real catalog
— "1000 views is high demand" is true until the marketplace triples — whereas a
percentile is self-calibrating.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

import numpy as np
from django.db.models import Sum
from django.utils import timezone

from .models import ProductDealScore, ProductPriceHistory

logger = logging.getLogger(__name__)


# ── Component weights ────────────────────────────────────────────────────────
W_DISCOUNT  = 0.40
W_DEMAND    = 0.25
W_QUALITY   = 0.20
W_SCARCITY  = 0.10
W_FRESHNESS = 0.05

#: A discount below this is not a deal, it is rounding.
MIN_DISCOUNT_PERCENT = 5.0

#: A discount above this is almost always a data-entry error or price inflation.
MAX_CREDIBLE_DISCOUNT = 90.0

#: Prior strength for the Bayesian rating — how many "average" reviews a product
#: is treated as starting with. Stops one 5★ review outranking forty 4.6★ ones.
RATING_PRIOR = 5.0

#: Stock at or below this reads as genuine scarcity rather than a thin listing.
SCARCITY_THRESHOLD = 20

#: Days of price history required before the credibility check has teeth.
MIN_HISTORY_DAYS = 5


def _percentile_rank(values: np.ndarray) -> np.ndarray:
    """
    Map raw values to 0–1 by rank within the pool.

    Rank rather than min-max: one product with 200x the views of everything else
    would otherwise compress every other product's demand score to nearly zero.
    """
    if values.size == 0:
        return values
    if values.size == 1:
        return np.array([1.0])
    order = values.argsort()
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(values.size)
    return ranks / (values.size - 1)


def snapshot_prices() -> int:
    """
    Record today's effective price for every published product.

    Cheap (one row per product per day) and it is the only thing that makes the
    credibility check possible — without a price history there is no way to tell
    a real markdown from an inflated reference price.
    """
    from product.models import Product

    today = timezone.now().date()
    products = Product.published.values_list('id', 'price', 'old_price')

    rows = [
        ProductPriceHistory(product_id=pid, date=today, price=price, old_price=old_price)
        for pid, price, old_price in products
    ]
    if not rows:
        return 0

    ProductPriceHistory.objects.bulk_create(rows, batch_size=2000, ignore_conflicts=True)
    logger.info("deals: snapshotted prices for %d products", len(rows))
    return len(rows)


def _price_credibility(product_ids: list[int], current_prices: dict[int, Decimal]) -> dict[int, float]:
    """
    How believable each product's advertised discount is, 0–1.

    Compares today's price against that product's own trailing 30 days. A price
    sitting at the bottom of its historical range is a real markdown and scores
    1.0; one sitting at the top is a markdown in name only and scores near 0.

    Products without enough history get 1.0 — a new listing gets the benefit of
    the doubt, and earns a real score once history accumulates.
    """
    since = timezone.now().date() - timedelta(days=30)

    history: dict[int, list[float]] = {}
    rows = (
        ProductPriceHistory.objects
        .filter(product_id__in=product_ids, date__gte=since)
        .values_list('product_id', 'price')
        .iterator(chunk_size=5000)
    )
    for product_id, price in rows:
        history.setdefault(product_id, []).append(float(price))

    credibility: dict[int, float] = {}
    for product_id in product_ids:
        prices = list(history.get(product_id, []))
        current = float(current_prices.get(product_id) or 0)

        if len(prices) < MIN_HISTORY_DAYS:
            credibility[product_id] = 1.0
            continue

        # Today's price belongs in the range. Without it, a product that has sat
        # at ₵100 for a month and dropped to ₵60 this morning looks like a flat
        # price line and scores as no markdown at all — the exact case the check
        # exists to reward. Today's snapshot may also not have been written yet.
        if current > 0:
            prices.append(current)

        low, high = min(prices), max(prices)
        if high <= low or current <= 0:
            # Price never moved: whatever discount is claimed against old_price
            # is a permanent fixture, not a deal event.
            credibility[product_id] = 0.25
            continue

        position = (current - low) / (high - low)          # 0 = cheapest ever, 1 = dearest
        credibility[product_id] = float(np.clip(1.0 - position, 0.0, 1.0))

    return credibility


def _live_flash_sales() -> dict[int, tuple[Decimal, Decimal]]:
    """{product_id: (sale_price, original_price)} for flash sales running right now."""
    from product.models import FlashSale

    now = timezone.now()
    rows = (
        FlashSale.objects
        .filter(is_active=True, start_time__lte=now, end_time__gte=now, product__isnull=False)
        .values_list('product_id', 'sale_price', 'original_price')
    )

    best: dict[int, tuple[Decimal, Decimal]] = {}
    for product_id, sale_price, original_price in rows:
        current = best.get(product_id)
        if current is None or sale_price < current[0]:
            best[product_id] = (sale_price, original_price)
    return best


def _stock_levels(product_ids: list[int]) -> dict[int, int]:
    """
    Sellable units per product, resolved in two bulk queries rather than
    Product.get_stock_quantity() per row.
    """
    from product.models import Product, Variants

    stock = dict(
        Product.objects.filter(id__in=product_ids)
        .values_list('id', 'total_quantity')
    )
    stock = {pid: int(qty or 0) for pid, qty in stock.items()}

    variant_totals = (
        Variants.objects.filter(product_id__in=product_ids)
        .values('product_id')
        .annotate(total=Sum('quantity'))
    )
    variant_map = {row['product_id']: int(row['total'] or 0) for row in variant_totals}

    variant_products = set(
        Product.objects.filter(id__in=product_ids)
        .exclude(variant='None')
        .values_list('id', flat=True)
    )
    for product_id in variant_products:
        stock[product_id] = variant_map.get(product_id, 0)

    return stock


def compute_deal_scores() -> int:
    """
    Rescore every published product's deal quality and replace ProductDealScore.

    Returns the number of eligible deals. Runs hourly — deals move fast enough
    that a daily cadence shows stale stock, and slowly enough that per-request
    scoring would be waste.
    """
    from django.db import transaction

    from product.models import Product

    products = list(
        Product.published
        .select_related('vendor')
        .only(
            'id', 'price', 'old_price', 'total_quantity', 'variant',
            'trending_score', 'avg_rating', 'review_count', 'date', 'vendor_id',
        )
    )
    if not products:
        logger.info("deals: no published products")
        return 0

    product_ids = [p.id for p in products]
    flash_sales = _live_flash_sales()
    stock_levels = _stock_levels(product_ids)
    current_prices = {p.id: p.price for p in products}
    credibility = _price_credibility(product_ids, current_prices)

    # np.mean of an empty list is nan, and nan is truthy — an `or 3.5` fallback
    # would silently poison every quality score on a catalog with no reviews yet.
    rated = [p.avg_rating or 0.0 for p in products if p.review_count]
    global_rating = float(np.mean(rated)) if rated else 3.5
    now = timezone.now()

    # ── Pass 1: facts, guardrails, raw signals ───────────────────────────────
    candidates = []
    ineligible: list[ProductDealScore] = []

    for product in products:
        flash = flash_sales.get(product.id)
        if flash:
            best_price, compare_at = flash
            has_flash = True
        else:
            best_price, compare_at = product.price, product.old_price
            has_flash = False

        stock = stock_levels.get(product.id, 0)

        discount = 0.0
        if compare_at and best_price is not None and compare_at > 0 and compare_at > best_price:
            discount = float((compare_at - best_price) / compare_at * 100)

        reason = ''
        if stock <= 0:
            reason = 'out of stock'
        elif discount < MIN_DISCOUNT_PERCENT:
            reason = 'no meaningful discount'
        elif discount > MAX_CREDIBLE_DISCOUNT:
            reason = f'discount above {MAX_CREDIBLE_DISCOUNT:.0f}% — likely a pricing error'

        record = dict(
            product_id=product.id,
            discount_percent=round(discount, 2),
            best_price=best_price,
            compare_at_price=compare_at,
            stock_remaining=stock,
            has_flash_sale=has_flash,
            price_percentile=round(credibility.get(product.id, 1.0), 4),
        )

        if reason:
            ineligible.append(ProductDealScore(
                **record, score=0.0, is_eligible=False, ineligible_reason=reason,
            ))
            continue

        candidates.append((product, record))

    if not candidates:
        with transaction.atomic():
            ProductDealScore.objects.all().delete()
            ProductDealScore.objects.bulk_create(ineligible, batch_size=2000)
        logger.info("deals: no eligible deals (%d products failed the guardrails)", len(ineligible))
        return 0

    # ── Pass 2: percentile-rank each signal within the candidate pool ────────
    discounts = np.array([record['discount_percent'] for _, record in candidates])
    credibilities = np.array([record['price_percentile'] for _, record in candidates])
    demand = np.array([float(p.trending_score or 0.0) for p, _ in candidates])
    stocks = np.array([float(record['stock_remaining']) for _, record in candidates])

    # Bayesian rating: shrink sparse review counts toward the catalog mean.
    quality = np.array([
        ((p.review_count or 0) * (p.avg_rating or 0.0) + RATING_PRIOR * global_rating)
        / ((p.review_count or 0) + RATING_PRIOR)
        for p, _ in candidates
    ])

    age_days = np.array([
        max((now - p.date).total_seconds() / 86400.0, 0.0) if p.date else 365.0
        for p, _ in candidates
    ])

    # A claimed discount counts only as far as the price history supports it.
    discount_rank = _percentile_rank(discounts) * credibilities
    demand_rank = _percentile_rank(demand)
    quality_rank = _percentile_rank(quality)
    # Scarcity is urgency, not absence: low-but-present stock scores highest.
    scarcity_rank = np.clip(1.0 - (stocks / SCARCITY_THRESHOLD), 0.0, 1.0)
    freshness_rank = np.exp(-age_days / 45.0)

    rows: list[ProductDealScore] = []
    for index, (_, record) in enumerate(candidates):
        discount_component = W_DISCOUNT * float(discount_rank[index])
        demand_component = W_DEMAND * float(demand_rank[index])
        quality_component = W_QUALITY * float(quality_rank[index])
        scarcity_component = W_SCARCITY * float(scarcity_rank[index])
        freshness_component = W_FRESHNESS * float(freshness_rank[index])

        total = (
            discount_component + demand_component + quality_component
            + scarcity_component + freshness_component
        )

        rows.append(ProductDealScore(
            **record,
            discount_component=round(discount_component, 6),
            demand_component=round(demand_component, 6),
            quality_component=round(quality_component, 6),
            scarcity_component=round(scarcity_component, 6),
            freshness_component=round(freshness_component, 6),
            score=round(total * 100, 4),          # 0–100 reads better in the admin
            is_eligible=True,
            ineligible_reason='',
        ))

    with transaction.atomic():
        ProductDealScore.objects.all().delete()
        ProductDealScore.objects.bulk_create(rows + ineligible, batch_size=2000)

    logger.info(
        "deals: scored %d eligible deals (%d products excluded by guardrails)",
        len(rows), len(ineligible),
    )
    return len(rows)

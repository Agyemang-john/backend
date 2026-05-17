"""
product/trending.py

Bulk trending-score computation.
Called by the `update_trending_scores` Celery task every 4 hours.

Score formula (all signals capped so one outlier can't dominate):

  score =
    5.0  × unique views last 24 h  (live from ProductViewLog)
    2.0  × unique views last 7 d   (from ProductDailyStats, bots excluded)
    0.5  × unique views last 30 d  (from ProductDailyStats, bots excluded)
   10.0  × cart adds last 7 d      (purchase-intent signal)
   20.0  × purchases last 7 d      (completed/processing orders)
    1.5  × avg_rating × log1p(review_count)   (quality baseline)

Recent activity is weighted much higher than historical totals so newly
trending products rise fast and stale ones decay automatically.
"""

import logging
from math import log1p
from datetime import timedelta, date as _date

from django.db.models import Count, Sum, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Signal weights ────────────────────────────────────────────────────────────
W_VIEWS_24H   =  5.0
W_VIEWS_7D    =  2.0
W_VIEWS_30D   =  0.5
W_CART_7D     = 10.0
W_PURCHASE_7D = 20.0
W_QUALITY     =  1.5


def compute_all_trending_scores() -> int:
    """
    Compute and bulk-update trending_score for every published product.
    Returns the number of products updated.
    """
    from product.models import Product, ProductViewLog, ProductDailyStats
    from order.models import CartItem, OrderProduct

    now      = timezone.now()
    today    = now.date()
    ago_24h  = now - timedelta(hours=24)
    ago_7d   = today - timedelta(days=7)
    ago_30d  = today - timedelta(days=30)

    # ── Signal 1: unique non-bot views in the last 24 hours (live table) ──────
    views_24h: dict[int, int] = dict(
        ProductViewLog.objects
        .filter(viewed_at__gte=ago_24h, is_bot=False, is_returning=False)
        .values('product_id')
        .annotate(cnt=Count('id'))
        .values_list('product_id', 'cnt')
    )

    # ── Signal 2: unique views last 7 days (daily-stats table, excl. today) ───
    views_7d: dict[int, int] = dict(
        ProductDailyStats.objects
        .filter(date__gte=ago_7d, date__lt=today)
        .values('product_id')
        .annotate(cnt=Sum('unique_views'))
        .values_list('product_id', 'cnt')
    )

    # ── Signal 3: unique views last 30 days (daily-stats table, excl. today) ──
    views_30d: dict[int, int] = dict(
        ProductDailyStats.objects
        .filter(date__gte=ago_30d, date__lt=today)
        .values('product_id')
        .annotate(cnt=Sum('unique_views'))
        .values_list('product_id', 'cnt')
    )

    # ── Signal 4: cart adds in last 7 days ───────────────────────────────────
    cart_7d: dict[int, int] = dict(
        CartItem.objects
        .filter(added__gte=ago_24h - timedelta(days=6))
        .values('product_id')
        .annotate(cnt=Count('id'))
        .values_list('product_id', 'cnt')
    )

    # ── Signal 5: purchases in last 7 days (confirmed orders only) ───────────
    purchases_7d: dict[int, int] = dict(
        OrderProduct.objects
        .filter(
            date_created__gte=ago_24h - timedelta(days=6),
            order__is_ordered=True,
            order__status__in=('processing', 'shipped', 'partially_delivered', 'delivered'),
        )
        .values('product_id')
        .annotate(cnt=Count('id'))
        .values_list('product_id', 'cnt')
    )

    # ── Fetch all published products (avg_rating / review_count denormalized) ─
    products = list(
        Product.objects.filter(status='published')
        .only('id', 'avg_rating', 'review_count')
    )

    # ── Compute and stage bulk updates ────────────────────────────────────────
    to_update: list[Product] = []
    for p in products:
        pid = p.id
        quality = (p.avg_rating or 0.0) * log1p(p.review_count or 0)
        score = (
            W_VIEWS_24H   * views_24h.get(pid,   0) +
            W_VIEWS_7D    * views_7d.get(pid,    0) +
            W_VIEWS_30D   * views_30d.get(pid,   0) +
            W_CART_7D     * cart_7d.get(pid,     0) +
            W_PURCHASE_7D * purchases_7d.get(pid, 0) +
            W_QUALITY     * quality
        )
        p.trending_score = round(score, 4)
        to_update.append(p)

    if to_update:
        Product.objects.bulk_update(to_update, ['trending_score'], batch_size=500)

    logger.info(
        "compute_all_trending_scores: updated %d products "
        "(24h_keys=%d, 7d_keys=%d, cart_keys=%d, purchase_keys=%d)",
        len(to_update), len(views_24h), len(views_7d), len(cart_7d), len(purchases_7d),
    )
    return len(to_update)

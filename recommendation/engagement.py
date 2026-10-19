"""
recommendation/engagement.py

Learning from what the rails showed and what shoppers did about it.

RecommendationEvent has been recording impressions and clicks since the rails
launched; this is the module that reads them back. Two signals come out:

* **Product engagement lift** — how much more (or less) often a product is
  clicked than an average product *shown in the same slot*. Raw CTR would reward
  whatever the ranker already put first, since slot 0 is clicked far more than
  slot 15 regardless of what sits there. Dividing clicks by the clicks expected
  at the positions it was shown in (COEC: clicks over expected clicks) removes
  that bias, so the lift measures the product, not its placement.

* **Skips** — products a shopper has been shown several times and never opened.
  Showing the same ignored tile every visit is the clearest sign a rail has
  stopped listening. Skipped products are pushed down, not hidden: interest
  changes, and an explicit "not interested" is what hides things for good.

Both are cheap aggregates over an indexed table, computed once per training run
(lift, skips for every shopper) or once per request for one shopper (skips).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import EVENT_ADD_TO_CART, EVENT_CLICK, EVENT_IMPRESSION, RecommendationEvent

logger = logging.getLogger(__name__)

#: Window for click-through statistics.
LIFT_DAYS = 30

#: Prior strength, in expected clicks. A product needs evidence worth this many
#: clicks before its lift moves far from 1.0 — one lucky click is not a signal.
LIFT_PRIOR = 5.0

#: Lift is clipped to this range so engagement nudges a ranking, never owns it.
LIFT_RANGE = (0.5, 2.0)

#: Positions beyond this share one bucket; there is too little traffic per slot.
MAX_POSITION_BUCKET = 30

#: A product shown this many times to one shopper without a click is "skipped".
SKIP_IMPRESSIONS = 3
SKIP_DAYS = 14

#: Score multiplier for a skipped product.
SKIP_PENALTY = 0.5


def _bucket(position) -> int:
    if position is None:
        return MAX_POSITION_BUCKET
    return min(int(position), MAX_POSITION_BUCKET)


def product_lift(days: int = LIFT_DAYS) -> dict[int, float]:
    """
    {product_id: engagement lift}, 1.0 = clicked as often as its slots predict.

    Only products with impressions appear; callers treat a missing product as 1.0.
    """
    since = timezone.now().date() - timedelta(days=days)
    rows = (
        RecommendationEvent.objects
        .filter(date__gte=since, event_type__in=(EVENT_IMPRESSION, EVENT_CLICK, EVENT_ADD_TO_CART))
        .values('product_id', 'position', 'event_type')
        .annotate(n=Count('id'))
    )

    impressions: dict[tuple[int, int], int] = defaultdict(int)     # (product, bucket) → shown
    clicks: dict[int, float] = defaultdict(float)
    slot_shown: dict[int, int] = defaultdict(int)
    slot_clicked: dict[int, int] = defaultdict(int)

    for row in rows:
        bucket = _bucket(row['position'])
        n = row['n']
        if row['event_type'] == EVENT_IMPRESSION:
            impressions[(row['product_id'], bucket)] += n
            slot_shown[bucket] += n
        else:
            # An add-to-cart from a rail is a stronger "yes" than a click.
            weight = 2.0 if row['event_type'] == EVENT_ADD_TO_CART else 1.0
            clicks[row['product_id']] += n * weight
            if row['event_type'] == EVENT_CLICK:
                slot_clicked[bucket] += n

    if not slot_shown:
        return {}

    total_shown = sum(slot_shown.values())
    global_ctr = (sum(slot_clicked.values()) + 1.0) / (total_shown + 10.0)
    # Per-slot click rate, shrunk toward the global rate where a slot is thin.
    slot_ctr = {
        bucket: (slot_clicked[bucket] + 20.0 * global_ctr) / (shown + 20.0)
        for bucket, shown in slot_shown.items()
    }

    expected: dict[int, float] = defaultdict(float)
    for (product_id, bucket), shown in impressions.items():
        expected[product_id] += shown * slot_ctr.get(bucket, global_ctr)

    low, high = LIFT_RANGE
    lift = {}
    for product_id, exp in expected.items():
        value = (clicks.get(product_id, 0.0) + LIFT_PRIOR) / (exp + LIFT_PRIOR)
        lift[product_id] = float(min(high, max(low, value)))

    logger.info("engagement: lift for %d products from %d impressions", len(lift), total_shown)
    return lift


def _skip_rows(queryset, *group_by: str):
    """(group…, product) rows shown SKIP_IMPRESSIONS+ times and never engaged with."""
    return (
        queryset
        .filter(date__gte=timezone.now().date() - timedelta(days=SKIP_DAYS))
        .values(*group_by, 'product_id')
        .annotate(
            shown=Count('id', filter=Q(event_type=EVENT_IMPRESSION)),
            engaged=Count('id', filter=Q(event_type__in=(EVENT_CLICK, EVENT_ADD_TO_CART))),
        )
        .filter(shown__gte=SKIP_IMPRESSIONS, engaged=0)
    )


def skipped_by_user() -> dict[int, set[int]]:
    """{user_id: {product_id, …}} for every signed-in shopper — one grouped query."""
    rows = _skip_rows(RecommendationEvent.objects.filter(user__isnull=False), 'user_id')
    skipped: dict[int, set[int]] = defaultdict(set)
    for row in rows.iterator(chunk_size=5000):
        skipped[row['user_id']].add(row['product_id'])
    return skipped


def skipped_for_request(request) -> set[int]:
    """Products this shopper (signed in or guest) keeps scrolling past."""
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        queryset = RecommendationEvent.objects.filter(user=user)
    else:
        from .serving import visitor_key
        key = visitor_key(request)
        if not key:
            return set()
        queryset = RecommendationEvent.objects.filter(visitor_key=key, user__isnull=True)
    return {row['product_id'] for row in _skip_rows(queryset)}

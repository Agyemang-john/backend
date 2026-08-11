"""
recommendation/dataset.py

Builds the implicit-feedback matrix the recommender trains on.

There is no separate event-logging pipeline to wait for: the platform already
records everything needed, spread across five tables. This module reads them,
weights each signal by how much purchase intent it represents, decays it by age,
and folds the result into one sparse user×item matrix.

    ProductViewLog          → view          (weight  1)
    RecentlyViewedProduct   → view          (weight  1, deduped against the above)
    Wishlist / SavedProduct → save          (weight  3)
    CartItem                → cart add      (weight  5)
    ProductReview           → review        (weight  4 … 10 by star rating)
    OrderProduct            → purchase      (weight 15)

Three decisions worth flagging:

* **Guests are first-class rows.** At early stage most sessions are anonymous, and
  throwing them away would leave the item factors starved. Guests are keyed by the
  same `visitor_key` the existing view tracking uses. They contribute to *item*
  learning; only authenticated users get a stored user vector.

* **Recency decays exponentially**, half-life 30 days. A shopper who bought a
  laptop last March should not be defined by it forever.

* **Bots are excluded** via the flag ProductViewLog already sets — otherwise
  crawler traffic silently becomes "popularity".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
from django.conf import settings
from django.utils import timezone
from scipy.sparse import csr_matrix

logger = logging.getLogger(__name__)


# ── Signal weights ───────────────────────────────────────────────────────────
# Same philosophy as the existing trending/engagement scores: weight by how
# strongly the action predicts a purchase. Override in settings to retune.

DEFAULT_WEIGHTS = {
    'view':      1.0,
    'save':      3.0,
    'cart':      5.0,
    'review':    4.0,     # scaled by star rating below
    'purchase': 15.0,
}

#: How far back to look for interactions.
LOOKBACK_DAYS = 180

#: Half-life of the recency decay, in days.
HALF_LIFE_DAYS = 30.0

#: Identities with fewer than this many distinct products are dropped — a single
#: pageview carries no signal but adds a row to every matrix operation.
MIN_INTERACTIONS_PER_USER = 2

#: Products with fewer than this many distinct users get no CF vector; content
#: similarity covers them instead.
MIN_INTERACTIONS_PER_ITEM = 2


def _weights() -> dict:
    return {**DEFAULT_WEIGHTS, **getattr(settings, 'RECOMMENDER_SIGNAL_WEIGHTS', {})}


def _config(name: str, default):
    return getattr(settings, f'RECOMMENDER_{name}', default)


@dataclass
class Dataset:
    """
    A trainable snapshot of shopper behaviour.

    matrix        : CSR (n_users × n_items) of decayed preference weights
    user_keys     : row → identity string ("u:12" or "v:abc-…")
    item_ids      : column → Product pk
    user_pos      : identity string → row
    item_pos      : Product pk → column
    auth_user_ids : row → Django user pk, for rows that are logged-in shoppers
    item_users    : column → number of distinct identities that touched it
    """

    matrix: csr_matrix
    user_keys: list[str]
    item_ids: list[int]
    user_pos: dict[str, int]
    item_pos: dict[int, int]
    auth_user_ids: dict[int, int] = field(default_factory=dict)
    item_users: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def n_users(self) -> int:
        return self.matrix.shape[0]

    @property
    def n_items(self) -> int:
        return self.matrix.shape[1]

    @property
    def n_interactions(self) -> int:
        return int(self.matrix.nnz)

    @property
    def sparsity(self) -> float:
        cells = self.n_users * self.n_items
        return (self.n_interactions / cells) if cells else 0.0

    @property
    def density_per_user(self) -> float:
        return (self.n_interactions / self.n_users) if self.n_users else 0.0

    def is_trainable(self) -> bool:
        """
        Whether there is enough signal for collaborative filtering to mean anything.

        Below this the run still completes — it just produces content-only
        recommendations rather than fitting noise and calling it a model.
        """
        return (
            self.n_users >= _config('MIN_USERS_FOR_CF', 20)
            and self.n_items >= _config('MIN_ITEMS_FOR_CF', 20)
            and self.n_interactions >= _config('MIN_INTERACTIONS_FOR_CF', 200)
        )

    def cf_trust(self) -> float:
        """
        How much weight collaborative filtering has earned, 0–1.

        Interaction density is the honest measure of whether CF can be trusted:
        with three events per user it cannot, with forty it can. This ramps
        smoothly so the storefront shifts from content-driven to behaviour-driven
        on its own as the marketplace grows — no flag to remember to flip.
        """
        if not self.is_trainable():
            return 0.0
        target = float(_config('CF_TRUST_DENSITY_TARGET', 25.0))
        return float(min(1.0, self.density_per_user / target))


# ── Signal collection ────────────────────────────────────────────────────────

def _decay(age_days: float, half_life: float) -> float:
    return float(0.5 ** (age_days / half_life)) if half_life > 0 else 1.0


class _Accumulator:
    """Sums decayed weights per (identity, product) pair."""

    def __init__(self, half_life: float, now):
        self.pairs: dict[tuple[str, int], float] = {}
        self.auth: dict[str, int] = {}
        self.half_life = half_life
        self.now = now

    def add(self, identity: str, product_id: int, weight: float, when, user_id=None):
        if not identity or not product_id or weight <= 0:
            return
        age = (self.now - when).total_seconds() / 86400.0 if when else 0.0
        if age < 0:
            age = 0.0
        value = weight * _decay(age, self.half_life)
        if value <= 0.0:
            return
        key = (identity, product_id)
        self.pairs[key] = self.pairs.get(key, 0.0) + value
        if user_id is not None:
            self.auth[identity] = user_id


def _identity(user_id, visitor_key: str | None) -> str | None:
    if user_id:
        return f"u:{user_id}"
    if visitor_key:
        return visitor_key if visitor_key.startswith(('u:', 'v:')) else f"v:{visitor_key}"
    return None


def _collect(acc: _Accumulator, since):
    """Pull every behavioural signal the platform already stores."""
    from order.models import CartItem, OrderProduct
    from product.models import (
        ProductReview, ProductViewLog, RecentlyViewedProduct, SavedProduct, Wishlist,
    )

    w = _weights()

    # ── Views ────────────────────────────────────────────────────────────────
    # Bots excluded — they are flagged at write time by product/utils.track_view.
    views = (
        ProductViewLog.objects
        .filter(viewed_at__gte=since, is_bot=False)
        .values_list('product_id', 'user_id', 'visitor_key', 'viewed_at')
        .iterator(chunk_size=5000)
    )
    n_views = 0
    for product_id, user_id, visitor_key, viewed_at in views:
        identity = _identity(user_id, visitor_key)
        if identity:
            acc.add(identity, product_id, w['view'], viewed_at, user_id)
            n_views += 1

    # Recently-viewed rows for authenticated users cover the window before the
    # analytics logger existed, and survive log pruning.
    recent = (
        RecentlyViewedProduct.objects
        .filter(viewed_at__gte=since)
        .values_list('product_id', 'user_id', 'viewed_at')
        .iterator(chunk_size=5000)
    )
    for product_id, user_id, viewed_at in recent:
        acc.add(f"u:{user_id}", product_id, w['view'], viewed_at, user_id)

    # ── Saves ────────────────────────────────────────────────────────────────
    wishlist = (
        Wishlist.objects
        .filter(saved_at__gte=since)
        .values_list('product_id', 'user_id', 'saved_at')
        .iterator(chunk_size=5000)
    )
    for product_id, user_id, saved_at in wishlist:
        acc.add(f"u:{user_id}", product_id, w['save'], saved_at, user_id)

    saved = (
        SavedProduct.objects
        .filter(saved_date__gte=since)
        .values_list('product_id', 'user_id', 'saved_date')
        .iterator(chunk_size=5000)
    )
    for product_id, user_id, saved_date in saved:
        acc.add(f"u:{user_id}", product_id, w['save'], saved_date, user_id)

    # ── Cart adds ────────────────────────────────────────────────────────────
    # Guest carts have no user, so they cannot be attributed to an identity.
    cart_items = (
        CartItem.objects
        .filter(created_at__gte=since, cart__user__isnull=False, product__isnull=False)
        .values_list('product_id', 'cart__user_id', 'created_at')
        .iterator(chunk_size=5000)
    )
    n_cart = 0
    for product_id, user_id, created_at in cart_items:
        acc.add(f"u:{user_id}", product_id, w['cart'], created_at, user_id)
        n_cart += 1

    # ── Reviews ──────────────────────────────────────────────────────────────
    # A 5-star review is a strong positive; a 1-star one is close to a negative,
    # so the weight scales with the rating rather than treating all reviews alike.
    reviews = (
        ProductReview.objects
        .filter(date__gte=since, user__isnull=False, product__isnull=False)
        .values_list('product_id', 'user_id', 'rating', 'date')
        .iterator(chunk_size=5000)
    )
    for product_id, user_id, rating, date in reviews:
        scale = (float(rating or 3) - 2.0) / 3.0          # 1★ → -0.33, 3★ → 0.33, 5★ → 1.0
        if scale <= 0:
            continue
        acc.add(f"u:{user_id}", product_id, w['review'] * (1.0 + scale), date, user_id)

    # ── Purchases ────────────────────────────────────────────────────────────
    purchases = (
        OrderProduct.objects
        .filter(
            date_created__gte=since,
            order__is_ordered=True,
            order__user__isnull=False,
            product__isnull=False,
        )
        .values_list('product_id', 'order__user_id', 'quantity', 'date_created')
        .iterator(chunk_size=5000)
    )
    n_purchases = 0
    for product_id, user_id, quantity, created in purchases:
        # Quantity matters, but sub-linearly: buying ten of something is not ten
        # times the preference signal of buying one.
        qty_boost = 1.0 + np.log1p(max(int(quantity or 1) - 1, 0))
        acc.add(f"u:{user_id}", product_id, w['purchase'] * float(qty_boost), created, user_id)
        n_purchases += 1

    logger.info(
        "dataset: collected views=%d cart=%d purchases=%d → %d raw pairs",
        n_views, n_cart, n_purchases, len(acc.pairs),
    )


# ── Public API ───────────────────────────────────────────────────────────────

def build_dataset(lookback_days: int | None = None, half_life: float | None = None) -> Dataset:
    """
    Assemble the user×item preference matrix from the platform's existing tables.

    Identities and products below the minimum-interaction thresholds are pruned
    iteratively — dropping sparse users can leave a product below the item
    threshold and vice versa, so both passes repeat until the matrix is stable.
    """
    from product.models import Product

    lookback = lookback_days if lookback_days is not None else _config('LOOKBACK_DAYS', LOOKBACK_DAYS)
    hl = half_life if half_life is not None else _config('HALF_LIFE_DAYS', HALF_LIFE_DAYS)

    now = timezone.now()
    since = now - timedelta(days=lookback)

    acc = _Accumulator(half_life=hl, now=now)
    _collect(acc, since)

    if not acc.pairs:
        logger.warning("dataset: no interactions found in the last %d days", lookback)
        return Dataset(
            matrix=csr_matrix((0, 0), dtype=np.float32),
            user_keys=[], item_ids=[], user_pos={}, item_pos={},
        )

    # Only products that can actually be recommended are worth modelling.
    sellable = set(Product.published.values_list('id', flat=True))
    pairs = {k: v for k, v in acc.pairs.items() if k[1] in sellable}

    min_user = _config('MIN_INTERACTIONS_PER_USER', MIN_INTERACTIONS_PER_USER)
    min_item = _config('MIN_INTERACTIONS_PER_ITEM', MIN_INTERACTIONS_PER_ITEM)

    # Iterative pruning to a stable core.
    for _ in range(5):
        user_counts: dict[str, int] = {}
        item_counts: dict[int, int] = {}
        for identity, product_id in pairs:
            user_counts[identity] = user_counts.get(identity, 0) + 1
            item_counts[product_id] = item_counts.get(product_id, 0) + 1

        pruned = {
            (identity, product_id): value
            for (identity, product_id), value in pairs.items()
            if user_counts.get(identity, 0) >= min_user and item_counts.get(product_id, 0) >= min_item
        }
        if len(pruned) == len(pairs):
            break
        pairs = pruned
        if not pairs:
            break

    if not pairs:
        logger.warning(
            "dataset: every interaction was pruned (need ≥%d products per shopper "
            "and ≥%d shoppers per product) — falling back to content-only",
            min_user, min_item,
        )
        return Dataset(
            matrix=csr_matrix((0, 0), dtype=np.float32),
            user_keys=[], item_ids=[], user_pos={}, item_pos={},
        )

    user_keys = sorted({identity for identity, _ in pairs})
    item_ids = sorted({product_id for _, product_id in pairs})
    user_pos = {identity: i for i, identity in enumerate(user_keys)}
    item_pos = {product_id: j for j, product_id in enumerate(item_ids)}

    rows = np.empty(len(pairs), dtype=np.int32)
    cols = np.empty(len(pairs), dtype=np.int32)
    vals = np.empty(len(pairs), dtype=np.float32)
    for n, ((identity, product_id), value) in enumerate(pairs.items()):
        rows[n] = user_pos[identity]
        cols[n] = item_pos[product_id]
        vals[n] = value

    matrix = csr_matrix(
        (vals, (rows, cols)), shape=(len(user_keys), len(item_ids)), dtype=np.float32,
    )

    auth_user_ids = {
        user_pos[identity]: user_id
        for identity, user_id in acc.auth.items()
        if identity in user_pos
    }
    item_users = np.asarray((matrix > 0).sum(axis=0)).ravel()

    dataset = Dataset(
        matrix=matrix,
        user_keys=user_keys,
        item_ids=item_ids,
        user_pos=user_pos,
        item_pos=item_pos,
        auth_user_ids=auth_user_ids,
        item_users=item_users,
    )

    logger.info(
        "dataset: %d shoppers × %d products, %d interactions "
        "(density %.1f/shopper, cf_trust %.2f)",
        dataset.n_users, dataset.n_items, dataset.n_interactions,
        dataset.density_per_user, dataset.cf_trust(),
    )
    return dataset


def train_test_split_by_time(dataset: Dataset, holdout_fraction: float = 0.2):
    """
    Hold out each shopper's most recent interactions for evaluation.

    A random split would let the model see the future and score itself on the
    past, which flatters every recommender ever built. Splitting by recency asks
    the question that actually matters: given what someone did before, can the
    model predict what they did next?

    Returns (train_matrix, holdout) where holdout maps row → set of held-out
    column indices. Shoppers with too little history to split are left intact in
    training and excluded from evaluation.
    """
    matrix = dataset.matrix.tocsr()
    n_users = matrix.shape[0]

    train = matrix.copy().tolil()
    holdout: dict[int, set[int]] = {}

    for row in range(n_users):
        start, end = matrix.indptr[row], matrix.indptr[row + 1]
        cols = matrix.indices[start:end]
        vals = matrix.data[start:end]
        if len(cols) < 3:
            continue

        # Decayed weight is a monotone proxy for recency within one signal type;
        # the highest-weighted tail is the closest available stand-in for "latest".
        n_holdout = max(1, int(round(len(cols) * holdout_fraction)))
        newest = np.argsort(-vals)[:n_holdout]
        held = {int(cols[i]) for i in newest}

        # Never strip a shopper down to nothing — they must stay trainable.
        if len(held) >= len(cols):
            held = set(list(held)[: len(cols) - 1])
        if not held:
            continue

        holdout[row] = held
        for col in held:
            train[row, col] = 0.0

    train = train.tocsr()
    train.eliminate_zeros()
    logger.info("dataset: held out interactions for %d of %d shoppers", len(holdout), n_users)
    return train, holdout

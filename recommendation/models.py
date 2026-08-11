"""
recommendation/models.py

Storage for the recommender's *output*. The learning itself lives in
als.py / content.py / similarity.py / ranker.py — these tables are the read
model the storefront serves from, plus the bookkeeping that makes the system
auditable.

  ModelRun            – one training run: version, metrics, timings. Serving
                        pins itself to the latest COMPLETED run, so a failed or
                        half-written retrain can never take the site down.
  ProductEmbedding    – per-product latent vectors (ALS collaborative + TF-IDF/SVD
                        content). Enables real-time similarity for guest sessions.
  UserEmbedding       – per-user ALS vector, for on-the-fly re-ranking.
  ProductNeighbor     – precomputed top-K similar items ("You might also like",
                        "Customers also bought").
  UserRecommendation  – precomputed ranked rail per user ("Recommended for you").
  ProductDealScore    – learned deal quality ("Today's Deals").
  RecommendationEvent – impressions/clicks on the rails. Measures CTR today and
                        becomes the training set for a learned ranker later.
  NotInterested       – explicit negative feedback; hard-filters a product out.

Vectors are stored as float32 `bytes` rather than JSON: 64 dims costs 256 bytes
instead of ~1.4 KB of text, and round-trips through numpy without precision loss.
Use the `.vector` / `.set_vector()` helpers rather than touching the raw field.
"""

import numpy as np
from django.conf import settings
from django.db import models
from django.utils import timezone


EMBEDDING_DTYPE = np.float32


def _decode(blob):
    """bytes → 1-D float32 ndarray. Returns None when unset or malformed."""
    if not blob:
        return None
    try:
        vector = np.frombuffer(bytes(blob), dtype=EMBEDDING_DTYPE)
    except (ValueError, TypeError):
        return None
    return vector if vector.size else None


def _encode(vec) -> bytes:
    return np.asarray(vec, dtype=EMBEDDING_DTYPE).tobytes()


# ── Training runs ────────────────────────────────────────────────────────────

class ModelRun(models.Model):
    """
    One execution of the training pipeline.

    Serving reads `ModelRun.objects.latest_completed()` and uses its id as a cache
    version key, so a new model goes live atomically the moment its run is marked
    completed — and an in-flight or crashed run is simply invisible.

    The metrics block is the honest answer to "is this thing working?": every run
    is scored against a held-out time split and against a popularity baseline.
    A run that loses to popularity is worth knowing about.
    """

    STATUS_RUNNING   = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED    = 'failed'
    STATUS_CHOICES = [
        (STATUS_RUNNING,   'Running'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED,    'Failed'),
    ]

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_RUNNING, db_index=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)

    # Dataset shape
    n_users = models.PositiveIntegerField(default=0)
    n_items = models.PositiveIntegerField(default=0)
    n_interactions = models.PositiveIntegerField(default=0)
    sparsity = models.FloatField(default=0.0, help_text='Fraction of the user×item matrix that is filled')

    # Hyperparameters actually used (they adapt to dataset size — see train.py)
    factors = models.PositiveSmallIntegerField(default=0)
    iterations = models.PositiveSmallIntegerField(default=0)
    regularization = models.FloatField(default=0.0)
    alpha = models.FloatField(default=0.0, help_text='Implicit-feedback confidence scaling')

    # Offline evaluation against a held-out time split
    precision_at_10 = models.FloatField(null=True, blank=True)
    recall_at_10 = models.FloatField(null=True, blank=True)
    map_at_10 = models.FloatField(null=True, blank=True)
    ndcg_at_10 = models.FloatField(null=True, blank=True)
    catalog_coverage = models.FloatField(
        null=True, blank=True, help_text='Share of the catalog that appears in anyone\'s top-10',
    )
    baseline_precision_at_10 = models.FloatField(
        null=True, blank=True, help_text='Most-popular baseline — the bar the model must clear',
    )

    # Blend state: how much weight collaborative filtering earned this run
    cf_weight = models.FloatField(
        default=0.0, help_text='0–1. Rises automatically as interaction density grows',
    )

    notes = models.TextField(blank=True)
    error = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Model Run'
        verbose_name_plural = 'Model Runs'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['status', '-finished_at'], name='rec_run_status_finished_idx'),
        ]

    def __str__(self):
        return f"Run #{self.pk} [{self.status}] {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def lift_over_baseline(self):
        """Relative improvement in precision@10 over the popularity baseline."""
        if not self.precision_at_10 or not self.baseline_precision_at_10:
            return None
        return (self.precision_at_10 - self.baseline_precision_at_10) / self.baseline_precision_at_10

    def mark_completed(self, **metrics):
        for field, value in metrics.items():
            setattr(self, field, value)
        self.status = self.STATUS_COMPLETED
        self.finished_at = timezone.now()
        self.duration_seconds = (self.finished_at - self.started_at).total_seconds()
        self.save()

    def mark_failed(self, error: str):
        self.status = self.STATUS_FAILED
        self.error = error[:5000]
        self.finished_at = timezone.now()
        self.duration_seconds = (self.finished_at - self.started_at).total_seconds()
        self.save(update_fields=['status', 'error', 'finished_at', 'duration_seconds'])


# ── Latent vectors ───────────────────────────────────────────────────────────

class ProductEmbedding(models.Model):
    """
    Two vectors per product, learned by different means and useful in different
    situations:

      cf_vector      – ALS latent factors. Encodes "who buys this", which is what
                       surfaces genuinely complementary products. Empty for items
                       with too little interaction history.
      content_vector – TF-IDF over title/brand/category/description, reduced by
                       truncated SVD. Always available, including for a product
                       listed five minutes ago. This is what carries the whole
                       system at early stage, before behavioural data exists.

    `interaction_count` records how much evidence stands behind cf_vector, which
    is what similarity.py uses to decide, per product, how far to trust CF over
    content.

    The two vectors carry different widths, and deliberately so: they are only
    ever compared within their own space, so each gets the rank that suits it —
    a small collaborative model that the interaction data can actually support,
    alongside a wide content model that text genuinely fills.
    """

    product = models.OneToOneField(
        'product.Product', on_delete=models.CASCADE, related_name='embedding',
    )
    cf_vector = models.BinaryField(null=True, blank=True, editable=False)
    content_vector = models.BinaryField(null=True, blank=True, editable=False)
    dim = models.PositiveSmallIntegerField(default=0, help_text='Width of cf_vector')
    content_dim = models.PositiveSmallIntegerField(default=0, help_text='Width of content_vector')
    interaction_count = models.PositiveIntegerField(
        default=0, help_text='Weighted interactions behind cf_vector — the confidence in it',
    )
    model_run = models.ForeignKey(
        ModelRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='product_embeddings',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Product Embedding'
        verbose_name_plural = 'Product Embeddings'
        indexes = [
            models.Index(fields=['model_run'], name='rec_pemb_run_idx'),
        ]

    def __str__(self):
        return f"Embedding for product {self.product_id}"

    @property
    def cf(self):
        return _decode(self.cf_vector)

    @property
    def content(self):
        return _decode(self.content_vector)

    def set_cf(self, vector):
        self.cf_vector = _encode(vector)
        self.dim = len(vector)

    def set_content(self, vector):
        self.content_vector = _encode(vector)
        self.content_dim = len(vector)


class UserEmbedding(models.Model):
    """
    A shopper's ALS latent vector — their position in the same space as
    ProductEmbedding.cf, so relevance is a dot product.

    Kept separately from UserRecommendation because it enables re-ranking against
    live context (what's in the cart right now, what they just viewed) without
    waiting for the next batch run.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='rec_embedding',
    )
    cf_vector = models.BinaryField(null=True, blank=True, editable=False)
    dim = models.PositiveSmallIntegerField(default=0)
    interaction_count = models.PositiveIntegerField(default=0)
    is_cold_start = models.BooleanField(
        default=True, db_index=True,
        help_text='Too little history to personalise — serving falls back to trending/deals',
    )
    model_run = models.ForeignKey(
        ModelRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='user_embeddings',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'User Embedding'
        verbose_name_plural = 'User Embeddings'

    def __str__(self):
        return f"Embedding for user {self.user_id}"

    @property
    def cf(self):
        return _decode(self.cf_vector)

    def set_cf(self, vector):
        self.cf_vector = _encode(vector)
        self.dim = len(vector)


# ── Item-to-item neighbours ──────────────────────────────────────────────────

KIND_HYBRID      = 'hybrid'
KIND_CF          = 'cf'
KIND_CONTENT     = 'content'
KIND_CO_PURCHASE = 'co_purchase'

NEIGHBOR_KINDS = [
    (KIND_HYBRID,      'Hybrid — blended CF + content'),
    (KIND_CF,          'Collaborative — co-interaction'),
    (KIND_CONTENT,     'Content — attribute similarity'),
    (KIND_CO_PURCHASE, 'Co-purchase — bought in the same order'),
]


class ProductNeighbor(models.Model):
    """
    Precomputed top-K nearest products, one row per (product, neighbour, kind).

    `hybrid` backs "You might also like this"; `co_purchase` backs "Customers also
    bought" and cart add-ons, which want complements (phone → case) rather than
    substitutes (phone → other phone).

    Only K neighbours per product are stored, so the table is O(products × K)
    instead of O(products²) — at 5k products and K=30 that's 150k rows, trivially
    indexed.
    """

    product = models.ForeignKey(
        'product.Product', on_delete=models.CASCADE, related_name='neighbors',
    )
    neighbor = models.ForeignKey(
        'product.Product', on_delete=models.CASCADE, related_name='neighbor_of',
    )
    kind = models.CharField(max_length=15, choices=NEIGHBOR_KINDS, default=KIND_HYBRID)
    score = models.FloatField(default=0.0)
    rank = models.PositiveSmallIntegerField(default=0)
    support = models.PositiveIntegerField(
        default=0, help_text='Users/orders behind this pairing — low support means low trust',
    )
    model_run = models.ForeignKey(
        ModelRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='neighbors',
    )

    class Meta:
        verbose_name = 'Product Neighbor'
        verbose_name_plural = 'Product Neighbors'
        unique_together = ('product', 'neighbor', 'kind')
        ordering = ['product_id', 'kind', 'rank']
        indexes = [
            models.Index(fields=['product', 'kind', 'rank'], name='rec_nbr_serve_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(product=models.F('neighbor')),
                name='rec_neighbor_not_self',
            ),
        ]

    def __str__(self):
        return f"{self.product_id} ~{self.kind}~ {self.neighbor_id} ({self.score:.3f})"


# ── Serving read model ───────────────────────────────────────────────────────

SURFACE_FOR_YOU       = 'recommended_for_you'
SURFACE_TODAYS_DEALS  = 'todays_deals'
SURFACE_ALSO_LIKE     = 'you_might_also_like'
SURFACE_ALSO_BOUGHT   = 'customers_also_bought'
SURFACE_CART_ADDONS   = 'cart_addons'
SURFACE_KEEP_SHOPPING = 'keep_shopping'
SURFACE_TRENDING      = 'trending'
SURFACE_SEARCH        = 'search'
SURFACE_OTHER         = 'other'

SURFACES = [
    (SURFACE_FOR_YOU,       'Recommended for you'),
    (SURFACE_TODAYS_DEALS,  "Today's Deals"),
    (SURFACE_ALSO_LIKE,     'You might also like this'),
    (SURFACE_ALSO_BOUGHT,   'Customers also bought'),
    (SURFACE_CART_ADDONS,   'Frequently bought together'),
    (SURFACE_KEEP_SHOPPING, 'Keep shopping for'),
    (SURFACE_TRENDING,      'Trending now'),
    (SURFACE_SEARCH,        'Search results'),
    (SURFACE_OTHER,         'Other'),
]

REASON_SIMILAR_TO_VIEWED = 'similar_to_viewed'
REASON_SIMILAR_TO_BOUGHT = 'similar_to_bought'
REASON_FROM_WISHLIST     = 'from_wishlist'
REASON_CATEGORY_AFFINITY = 'category_affinity'
REASON_BRAND_AFFINITY    = 'brand_affinity'
REASON_COLLABORATIVE     = 'collaborative'
REASON_DEAL              = 'deal'
REASON_TRENDING          = 'trending'
REASON_TOP_RATED         = 'top_rated'
REASON_POPULAR           = 'popular'

REASONS = [
    (REASON_SIMILAR_TO_VIEWED, 'Because you viewed'),
    (REASON_SIMILAR_TO_BOUGHT, 'Because you bought'),
    (REASON_FROM_WISHLIST,     'Based on your wishlist'),
    (REASON_CATEGORY_AFFINITY, 'From a category you shop'),
    (REASON_BRAND_AFFINITY,    'From a brand you like'),
    (REASON_COLLABORATIVE,     'Shoppers like you also bought'),
    (REASON_DEAL,              'On deal right now'),
    (REASON_TRENDING,          'Trending on Negromart'),
    (REASON_TOP_RATED,         'Highly rated'),
    (REASON_POPULAR,           'Popular right now'),
]


class UserRecommendation(models.Model):
    """
    The materialised "Recommended for you" rail: scored offline, read at request
    time with a single indexed lookup on (user, surface, rank).

    Each row carries a machine-readable `reason` plus a shopper-facing
    `reason_detail` ("Because you viewed Nike Air Max 90") — Amazon-style
    explanations, which measurably outperform unexplained rails and make the
    system debuggable when a recommendation looks wrong.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recommendations',
    )
    product = models.ForeignKey(
        'product.Product', on_delete=models.CASCADE, related_name='recommended_to',
    )
    surface = models.CharField(max_length=30, choices=SURFACES, default=SURFACE_FOR_YOU)
    score = models.FloatField(default=0.0)
    rank = models.PositiveSmallIntegerField(default=0)
    reason = models.CharField(max_length=30, choices=REASONS, default=REASON_POPULAR)
    reason_detail = models.CharField(max_length=200, blank=True)
    source_product = models.ForeignKey(
        'product.Product', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        help_text='Seed item this recommendation was generated from',
    )
    model_run = models.ForeignKey(
        ModelRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='recommendations',
    )
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'User Recommendation'
        verbose_name_plural = 'User Recommendations'
        unique_together = ('user', 'product', 'surface')
        ordering = ['rank']
        indexes = [
            models.Index(fields=['user', 'surface', 'rank'], name='rec_urec_serve_idx'),
            models.Index(fields=['generated_at'], name='rec_urec_generated_idx'),
        ]

    def __str__(self):
        return f"{self.user_id} → {self.product_id} [{self.surface} #{self.rank}]"


class ProductDealScore(models.Model):
    """
    Learned quality score for "Today's Deals".

    Ranking deals purely by discount percentage rewards a seller who inflates
    `old_price` on something nobody wants. This model scores a deal the way a
    merchandiser would — depth of discount, demand, product quality, scarcity,
    freshness — and keeps each component stored separately so a ranking can be
    explained and retuned rather than argued about.

    `price_percentile` compares the current price against this product's own
    30-day price history, which is the anti-gaming check: a "50% off" on an item
    whose price was raised last week scores as no discount at all.
    """

    product = models.OneToOneField(
        'product.Product', on_delete=models.CASCADE, related_name='deal_score',
    )

    # Shopper-visible facts
    discount_percent = models.FloatField(default=0.0)
    best_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Lowest live price in GHS — the flash-sale price when one is running',
    )
    compare_at_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    stock_remaining = models.PositiveIntegerField(default=0)
    has_flash_sale = models.BooleanField(default=False)

    # Score components — the audit trail behind `score`
    discount_component = models.FloatField(default=0.0)
    demand_component = models.FloatField(default=0.0)
    quality_component = models.FloatField(default=0.0)
    scarcity_component = models.FloatField(default=0.0)
    freshness_component = models.FloatField(default=0.0)
    price_percentile = models.FloatField(
        default=1.0, help_text='0–1: where the current price sits in this product\'s own 30-day range',
    )

    score = models.FloatField(default=0.0, db_index=True)
    is_eligible = models.BooleanField(default=False, db_index=True)
    ineligible_reason = models.CharField(max_length=100, blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Product Deal Score'
        verbose_name_plural = 'Product Deal Scores'
        ordering = ['-score']
        indexes = [
            models.Index(fields=['is_eligible', '-score'], name='rec_deal_serve_idx'),
        ]

    def __str__(self):
        return f"Deal {self.score:.1f} · product {self.product_id}"

    @property
    def savings(self):
        if self.compare_at_price and self.best_price:
            return max(self.compare_at_price - self.best_price, 0)
        return None


class ProductPriceHistory(models.Model):
    """
    Daily snapshot of a product's effective price.

    Exists for one reason: without price history you cannot tell a real discount
    from an inflated `old_price`. ProductDealScore.price_percentile reads this to
    verify that "40% off" means the price actually fell.
    """

    product = models.ForeignKey(
        'product.Product', on_delete=models.CASCADE, related_name='price_history',
    )
    date = models.DateField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    old_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = 'Product Price History'
        verbose_name_plural = 'Product Price History'
        unique_together = ('product', 'date')
        ordering = ['-date']
        indexes = [
            models.Index(fields=['product', '-date'], name='rec_price_hist_idx'),
        ]

    def __str__(self):
        return f"{self.product_id} @ {self.price} on {self.date}"


# ── Feedback loop ────────────────────────────────────────────────────────────

EVENT_IMPRESSION = 'impression'
EVENT_CLICK      = 'click'
EVENT_ADD_TO_CART = 'add_to_cart'
EVENT_PURCHASE   = 'purchase'

REC_EVENT_TYPES = [
    (EVENT_IMPRESSION,  'Impression — rendered in a rail'),
    (EVENT_CLICK,       'Click — opened from a rail'),
    (EVENT_ADD_TO_CART, 'Added to cart from a rail'),
    (EVENT_PURCHASE,    'Purchased after a rail impression'),
]


class RecommendationEvent(models.Model):
    """
    What the rails showed and what the shopper did about it.

    Two jobs. Today: CTR and add-to-cart rate per surface, so "Today's Deals vs.
    Recommended for you" is a measurement rather than an opinion. Later: this is
    the labelled training set for a learned ranker — `position` is recorded
    precisely so position bias can be corrected for when that time comes.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='recommendation_events',
    )
    visitor_key = models.CharField(
        max_length=100, blank=True, db_index=True,
        help_text='"v:{uuid}" for guests — the same identity scheme as view tracking',
    )
    product = models.ForeignKey(
        'product.Product', on_delete=models.CASCADE, related_name='recommendation_events',
    )
    surface = models.CharField(max_length=30, choices=SURFACES)
    event_type = models.CharField(max_length=15, choices=REC_EVENT_TYPES)
    position = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='0-based slot in the rail — needed to debias the ranker',
    )
    reason = models.CharField(max_length=30, blank=True)
    model_run = models.ForeignKey(
        ModelRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='events',
    )
    date = models.DateField(db_index=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Recommendation Event'
        verbose_name_plural = 'Recommendation Events'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['surface', 'event_type', 'date'], name='rec_ev_ctr_idx'),
            models.Index(fields=['user', '-created_at'], name='rec_ev_user_idx'),
            models.Index(fields=['product', 'event_type', 'date'], name='rec_ev_product_idx'),
        ]

    def __str__(self):
        return f"{self.surface}/{self.event_type} product={self.product_id}"

    def save(self, *args, **kwargs):
        if not self.date:
            self.date = (self.created_at or timezone.now()).date()
        super().save(*args, **kwargs)


class NotInterested(models.Model):
    """
    Explicit "don't show me this" — a hard filter applied at serving time.

    Implicit feedback can only ever infer disinterest; this records it directly,
    and one signal here outweighs any amount of accidental browsing.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name='not_interested',
    )
    visitor_key = models.CharField(max_length=100, blank=True, db_index=True)
    product = models.ForeignKey(
        'product.Product', on_delete=models.CASCADE, related_name='not_interested_by',
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Not Interested'
        verbose_name_plural = 'Not Interested'
        indexes = [
            models.Index(fields=['user'], name='rec_notint_user_idx'),
            models.Index(fields=['visitor_key', 'product'], name='rec_notint_guest_idx'),
        ]
        constraints = [
            # Two partial constraints rather than one unique_together across
            # (user, visitor_key, product). Postgres treats NULLs as distinct, so
            # a plain unique_together would not constrain guests at all — every
            # dismissal from a signed-out visitor would insert another row.
            models.UniqueConstraint(
                fields=['user', 'product'],
                condition=models.Q(user__isnull=False),
                name='rec_notint_unique_user',
            ),
            models.UniqueConstraint(
                fields=['visitor_key', 'product'],
                condition=models.Q(user__isnull=True),
                name='rec_notint_unique_guest',
            ),
        ]

    def __str__(self):
        return f"{self.user_id or self.visitor_key} ✕ {self.product_id}"

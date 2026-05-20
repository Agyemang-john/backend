import re
import logging
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

RECENTLY_VIEWED_MAX = getattr(settings, "RECENTLY_VIEWED_MAX", 10)
VIEW_DEDUP_TTL      = getattr(settings, "VIEW_DEDUP_TTL", 86400)        # 24 h
RECENT_LIST_TTL     = getattr(settings, "RECENT_LIST_TTL", 2592000)     # 30 days
VIEW_BUF_TTL        = getattr(settings, "VIEW_BUF_TTL", 172800)         # 48 h safety TTL
RETURN_WINDOW_TTL   = getattr(settings, "RETURN_WINDOW_TTL", 2592000)   # 30 days for return detection

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

# Patterns that strongly indicate a crawler/bot user-agent
_BOT_RE = re.compile(
    r'bot|crawl|spider|slurp|prerender|headless|puppet|playwright|'
    r'selenium|chrome-lighthouse|facebookexternalhit|twitterbot|'
    r'linkedinbot|whatsapp|applebot|bingbot|googlebot|yandex|baidu|'
    r'duckduck|semrush|ahrefs|moz/|wget|curl|python-requests|java/',
    re.IGNORECASE,
)

_MOBILE_RE  = re.compile(r'mobile|android|iphone|ipod|blackberry|opera mini|iemobile', re.IGNORECASE)
_TABLET_RE  = re.compile(r'ipad|tablet|kindle|playbook|silk', re.IGNORECASE)


# ── Safe Redis connection ────────────────────────────────────────────────────

def _get_redis():
    try:
        from django_redis import get_redis_connection
        return get_redis_connection("default")
    except Exception:
        logger.warning("Redis unavailable — view tracking skipped")
        return None


# ── Visitor identity ─────────────────────────────────────────────────────────

def _get_visitor_id(request) -> str | None:
    """
    Returns a stable visitor identifier or None.
    Priority: X-Visitor-ID header → visitor_id cookie → authenticated user id.
    """
    visitor_id = request.headers.get('X-Visitor-ID', '').strip()
    if visitor_id and _UUID_RE.match(visitor_id):
        return f"v:{visitor_id}"

    cookie_vid = request.COOKIES.get('visitor_id', '').strip()
    if cookie_vid and _UUID_RE.match(cookie_vid):
        return f"v:{cookie_vid}"

    if request.user.is_authenticated:
        return f"u:{request.user.pk}"

    return None


def _dedup_key(request, product_id: int) -> str | None:
    vid = _get_visitor_id(request)
    if vid is None:
        return None
    return f"view:dedup:{vid}:{product_id}"


def _recent_key(request) -> str | None:
    vid = _get_visitor_id(request)
    if vid is None:
        return None
    return f"recent:{vid}"


# ── Bot & device detection ───────────────────────────────────────────────────

def detect_bot(request) -> bool:
    ua = request.headers.get('User-Agent', '')
    return bool(_BOT_RE.search(ua))


def detect_device(request) -> str:
    ua = request.headers.get('User-Agent', '')
    if _TABLET_RE.search(ua):
        return 'tablet'
    if _MOBILE_RE.search(ua):
        return 'mobile'
    if ua:
        return 'desktop'
    return 'unknown'


# ── View count — Redis buffer ────────────────────────────────────────────────

def is_new_view(request, product_id: int) -> bool:
    """
    Returns True if this is the first view of product_id within the dedup window.
    Kept for backwards compatibility — prefer track_view() for new code.
    """
    key = _dedup_key(request, product_id)
    if key is None:
        return False
    conn = _get_redis()
    if conn is None:
        return False
    try:
        return bool(conn.set(key, 1, nx=True, ex=VIEW_DEDUP_TTL))
    except Exception:
        logger.warning("is_new_view: Redis error for product %s", product_id)
        return False


FLUSH_LOCK_TTL = getattr(settings, "FLUSH_LOCK_TTL", 200)
FLUSH_LOCK_KEY = "views:flush:lock"


def buffer_view_count(product_id: int) -> None:
    """
    Increment the Redis view-count buffer and self-schedule a flush if needed.
    """
    conn = _get_redis()
    if conn is None:
        try:
            from django.db.models import F
            from product.models import Product
            Product.objects.filter(id=product_id).update(views=F('views') + 1)
        except Exception:
            logger.error("buffer_view_count: DB fallback failed for product %s", product_id)
        return
    try:
        pipe = conn.pipeline()
        pipe.incr(f"views:buf:{product_id}")
        pipe.expire(f"views:buf:{product_id}", VIEW_BUF_TTL)
        pipe.set(FLUSH_LOCK_KEY, 1, nx=True, ex=FLUSH_LOCK_TTL)
        results = pipe.execute()
        flush_needed = bool(results[2])
    except Exception:
        logger.warning("buffer_view_count: Redis error for product %s", product_id)
        return

    if flush_needed:
        try:
            from product.tasks import flush_view_counts
            flush_view_counts.apply_async(countdown=FLUSH_LOCK_TTL)
        except Exception:
            logger.warning("buffer_view_count: could not schedule flush_view_counts")


# ── Unified view tracking ────────────────────────────────────────────────────

def track_view(request, product_id: int) -> None:
    """
    Single entry point for all view tracking. Non-blocking — analytics writes
    are queued as Celery tasks so the product detail response is never delayed.

    Steps:
      1. Deduplication: 24h Redis SET NX — skip if same visitor within 24h.
      2. Returning visitor: 30d Redis SET NX — True if key already existed.
      3. Bot detection: bots are logged but NOT counted in product.views.
      4. buffer_view_count(): increments Redis buffer (flushed to DB every 3 min).
      5. log_view_event.delay(): async Celery task writes one ProductViewLog row.

    RecentlyViewedProduct DB sync is intentionally NOT done here — it lives in
    ProductDetailAPIView so it fires on every page load and keeps ordering correct.
    """
    visitor_key = _get_visitor_id(request)
    if visitor_key is None:
        return

    conn = _get_redis()
    if conn is None:
        return

    # 1. Dedup check (24 h)
    dedup_key = f"view:dedup:{visitor_key}:{product_id}"
    try:
        is_new = bool(conn.set(dedup_key, 1, nx=True, ex=VIEW_DEDUP_TTL))
    except Exception:
        logger.warning("track_view: dedup Redis error product=%s", product_id)
        return

    if not is_new:
        return  # same visitor within 24 h — pure duplicate

    # 2. Returning visitor check (30 d window)
    try:
        return_key = f"view:first:{visitor_key}:{product_id}"
        first_in_window = bool(conn.set(return_key, 1, nx=True, ex=RETURN_WINDOW_TTL))
        is_returning = not first_in_window
    except Exception:
        is_returning = False

    # 3. Bot detection
    is_bot = detect_bot(request)
    device = detect_device(request)

    # 4. Count only non-bot views in the main product.views integer
    if not is_bot:
        buffer_view_count(product_id)

    # 5. Async log to ProductViewLog
    user_id = request.user.pk if request.user.is_authenticated else None
    try:
        from product.tasks import log_view_event
        log_view_event.delay(
            product_id=product_id,
            visitor_key=visitor_key,
            user_id=user_id,
            is_bot=is_bot,
            is_returning=is_returning,
            device_type=device,
            date_str=timezone.now().date().isoformat(),
        )
    except Exception:
        logger.warning("track_view: could not enqueue log_view_event for product %s", product_id)


# ── Recently viewed ──────────────────────────────────────────────────────────

def update_recently_viewed(request, product_id: int) -> None:
    """
    Prepend product_id to the visitor's recently-viewed Redis list.
    Deduplicates and caps at RECENTLY_VIEWED_MAX entries.
    """
    key = _recent_key(request)
    if key is None:
        return
    conn = _get_redis()
    if conn is None:
        return
    try:
        pipe = conn.pipeline()
        pipe.lrem(key, 0, product_id)
        pipe.lpush(key, product_id)
        pipe.ltrim(key, 0, RECENTLY_VIEWED_MAX - 1)
        pipe.expire(key, RECENT_LIST_TTL)
        pipe.execute()
    except Exception:
        logger.warning("update_recently_viewed: Redis error for key %s", key)


def get_recently_viewed_ids(request, limit: int = RECENTLY_VIEWED_MAX) -> list[int]:
    """Return ordered list of recently viewed product IDs, newest first."""
    key = _recent_key(request)
    if key is None:
        return []
    conn = _get_redis()
    if conn is None:
        return []
    try:
        raw = conn.lrange(key, 0, limit - 1)
        return [int(v) for v in raw]
    except Exception:
        logger.warning("get_recently_viewed_ids: Redis error for key %s", key)
        return []


def get_recently_viewed_products(request, limit: int = RECENTLY_VIEWED_MAX):
    """Return a queryset of Product objects in recency order."""
    from product.models import Product
    from django.db.models import Case, When

    ids = get_recently_viewed_ids(request, limit)
    if not ids:
        return Product.objects.none()

    ordering = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(ids)])
    return Product.published.filter(pk__in=ids).order_by(ordering)


def clear_recently_viewed(request) -> None:
    """Delete the entire recently-viewed list for this visitor."""
    key = _recent_key(request)
    if key is None:
        return
    conn = _get_redis()
    if conn is None:
        return
    try:
        conn.delete(key)
    except Exception:
        pass


def remove_recently_viewed(request, product_id: int) -> None:
    """Remove one product from the recently-viewed list."""
    key = _recent_key(request)
    if key is None:
        return
    conn = _get_redis()
    if conn is None:
        return
    try:
        conn.lrem(key, 0, product_id)
    except Exception:
        pass


# ── Brand view tracking ──────────────────────────────────────────────────────

_BRAND_FLUSH_LOCK_KEY = "brand:views:flush:lock"


def buffer_brand_view_count(brand_id: int) -> None:
    """
    Increment the Redis brand view-count buffer.
    Falls back to a direct DB update if Redis is unavailable.
    The Celery Beat task flush_brand_view_counts() drains this buffer every 3 min.
    """
    conn = _get_redis()
    if conn is None:
        try:
            from django.db.models import F
            from product.models import Brand
            Brand.objects.filter(id=brand_id).update(views=F('views') + 1)
        except Exception:
            logger.error("buffer_brand_view_count: DB fallback failed for brand %s", brand_id)
        return
    try:
        pipe = conn.pipeline()
        pipe.incr(f"brand:views:buf:{brand_id}")
        pipe.expire(f"brand:views:buf:{brand_id}", VIEW_BUF_TTL)
        pipe.set(_BRAND_FLUSH_LOCK_KEY, 1, nx=True, ex=FLUSH_LOCK_TTL)
        results = pipe.execute()
        flush_needed = bool(results[2])
    except Exception:
        logger.warning("buffer_brand_view_count: Redis error for brand %s", brand_id)
        return

    if flush_needed:
        try:
            from product.tasks import flush_brand_view_counts
            flush_brand_view_counts.apply_async(countdown=FLUSH_LOCK_TTL)
        except Exception:
            logger.warning("buffer_brand_view_count: could not schedule flush_brand_view_counts")


def track_brand_view(request, brand_id: int) -> None:
    """
    Track one brand page visit. Deduped per visitor per 24 h via Redis SET NX.
    Non-bot views are buffered in Redis and flushed to Brand.views every 3 min.
    Bots are silently skipped — they don't influence engagement scores.
    """
    visitor_key = _get_visitor_id(request)
    if visitor_key is None:
        return

    conn = _get_redis()
    if conn is None:
        return

    dedup_key = f"brand:view:dedup:{visitor_key}:{brand_id}"
    try:
        is_new = bool(conn.set(dedup_key, 1, nx=True, ex=VIEW_DEDUP_TTL))
    except Exception:
        logger.warning("track_brand_view: dedup Redis error brand=%s", brand_id)
        return

    if not is_new:
        return  # same visitor within 24 h — skip

    if not detect_bot(request):
        buffer_brand_view_count(brand_id)


# ── Subcategory view tracking ─────────────────────────────────────────────────

_CAT_FLUSH_LOCK_KEY = "cat:views:flush:lock"


def buffer_subcategory_view_count(subcategory_id: int) -> None:
    """
    Increment the Redis subcategory view-count buffer.
    Falls back to a direct DB update if Redis is unavailable.
    The Celery Beat task flush_subcategory_view_counts() drains this buffer every 3 min.
    """
    conn = _get_redis()
    if conn is None:
        try:
            from django.db.models import F
            from product.models import Sub_Category
            Sub_Category.objects.filter(id=subcategory_id).update(views=F('views') + 1)
        except Exception:
            logger.error("buffer_subcategory_view_count: DB fallback failed for subcategory %s", subcategory_id)
        return
    try:
        pipe = conn.pipeline()
        pipe.incr(f"cat:views:buf:{subcategory_id}")
        pipe.expire(f"cat:views:buf:{subcategory_id}", VIEW_BUF_TTL)
        pipe.set(_CAT_FLUSH_LOCK_KEY, 1, nx=True, ex=FLUSH_LOCK_TTL)
        results = pipe.execute()
        flush_needed = bool(results[2])
    except Exception:
        logger.warning("buffer_subcategory_view_count: Redis error for subcategory %s", subcategory_id)
        return

    if flush_needed:
        try:
            from product.tasks import flush_subcategory_view_counts
            flush_subcategory_view_counts.apply_async(countdown=FLUSH_LOCK_TTL)
        except Exception:
            logger.warning("buffer_subcategory_view_count: could not schedule flush_subcategory_view_counts")


def track_subcategory_view(request, subcategory_id: int) -> None:
    """
    Track one subcategory page visit. Deduped per visitor per 24 h via Redis SET NX.
    Non-bot views are buffered in Redis and flushed to Sub_Category.views every 3 min.
    """
    visitor_key = _get_visitor_id(request)
    if visitor_key is None:
        return

    conn = _get_redis()
    if conn is None:
        return

    dedup_key = f"cat:view:dedup:{visitor_key}:{subcategory_id}"
    try:
        is_new = bool(conn.set(dedup_key, 1, nx=True, ex=VIEW_DEDUP_TTL))
    except Exception:
        logger.warning("track_subcategory_view: dedup Redis error subcategory=%s", subcategory_id)
        return

    if not is_new:
        return  # same visitor within 24 h — skip

    if not detect_bot(request):
        buffer_subcategory_view_count(subcategory_id)


# ── Category view tracking ────────────────────────────────────────────────────

_MAINCAT_FLUSH_LOCK_KEY = "maincat:views:flush:lock"


def buffer_category_view_count(category_id: int) -> None:
    """
    Increment the Redis category view-count buffer.
    Falls back to a direct DB update if Redis is unavailable.
    The Celery Beat task flush_category_view_counts() drains this buffer every 3 min.
    """
    conn = _get_redis()
    if conn is None:
        try:
            from django.db.models import F
            from product.models import Category
            Category.objects.filter(id=category_id).update(views=F('views') + 1)
        except Exception:
            logger.error("buffer_category_view_count: DB fallback failed for category %s", category_id)
        return
    try:
        pipe = conn.pipeline()
        pipe.incr(f"maincat:views:buf:{category_id}")
        pipe.expire(f"maincat:views:buf:{category_id}", VIEW_BUF_TTL)
        pipe.set(_MAINCAT_FLUSH_LOCK_KEY, 1, nx=True, ex=FLUSH_LOCK_TTL)
        results = pipe.execute()
        flush_needed = bool(results[2])
    except Exception:
        logger.warning("buffer_category_view_count: Redis error for category %s", category_id)
        return

    if flush_needed:
        try:
            from product.tasks import flush_category_view_counts
            flush_category_view_counts.apply_async(countdown=FLUSH_LOCK_TTL)
        except Exception:
            logger.warning("buffer_category_view_count: could not schedule flush_category_view_counts")


def track_category_view(request, category_id: int) -> None:
    """
    Track one Category page visit. Deduped per visitor per 24 h via Redis SET NX.
    Non-bot views are buffered in Redis and flushed to Category.views every 3 min.
    Bots are silently skipped — they don't influence engagement scores.
    """
    visitor_key = _get_visitor_id(request)
    if visitor_key is None:
        return

    conn = _get_redis()
    if conn is None:
        return

    dedup_key = f"maincat:view:dedup:{visitor_key}:{category_id}"
    try:
        is_new = bool(conn.set(dedup_key, 1, nx=True, ex=VIEW_DEDUP_TTL))
    except Exception:
        logger.warning("track_category_view: dedup Redis error category=%s", category_id)
        return

    if not is_new:
        return  # same visitor within 24 h — skip

    if not detect_bot(request):
        buffer_category_view_count(category_id)


# ── GeoIP ────────────────────────────────────────────────────────────────────

def get_region_with_geoip(ip):
    try:
        from django.contrib.gis.geoip2 import GeoIP2
        geo = GeoIP2()
        return geo.city(ip)['region']
    except Exception as e:
        logger.debug("GeoIP2 error: %s", e)
        return None


def calculate_packaging_fee(weight, volume):
    weight_rate = 1.0
    volume_rate = 1.0
    return (weight * weight_rate) + (volume * volume_rate)

import re
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

RECENTLY_VIEWED_MAX = getattr(settings, "RECENTLY_VIEWED_MAX", 10)
VIEW_DEDUP_TTL      = getattr(settings, "VIEW_DEDUP_TTL", 86400)        # 24 h
RECENT_LIST_TTL     = getattr(settings, "RECENT_LIST_TTL", 2592000)     # 30 days
VIEW_BUF_TTL        = getattr(settings, "VIEW_BUF_TTL", 172800)         # 48 h safety TTL

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


# ── Safe Redis connection ────────────────────────────────────────────────────

def _get_redis():
    """
    Returns a raw Redis connection or None if Redis is unavailable.
    Never raises — callers must check for None before use.
    """
    try:
        from django_redis import get_redis_connection
        return get_redis_connection("default")
    except Exception:
        logger.warning("Redis unavailable — view tracking skipped")
        return None


# ── Visitor identity ─────────────────────────────────────────────────────────

def _get_visitor_id(request) -> str | None:
    """
    Returns a stable, validated visitor identifier string, or None.

    Priority:
      1. X-Visitor-ID header — sent by client-side requests and by the
         Next.js SSR layer (createServerAxios extracts the cookie value
         and forwards it as a header).
      2. visitor_id cookie — fallback for SSR requests that use plain
         fetch() and only forward the raw Cookie header.
      3. Authenticated user ID.
      4. None — skip tracking.
    """
    # 1. Explicit header (client-side axios + createServerAxios)
    visitor_id = request.headers.get('X-Visitor-ID', '').strip()
    if visitor_id and _UUID_RE.match(visitor_id):
        return f"v:{visitor_id}"

    # 2. Cookie fallback (plain SSR fetch that only forwards Cookie header)
    cookie_vid = request.COOKIES.get('visitor_id', '').strip()
    if cookie_vid and _UUID_RE.match(cookie_vid):
        return f"v:{cookie_vid}"

    # 3. Authenticated user
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


# ── View count — Redis buffer ────────────────────────────────────────────────

def is_new_view(request, product_id: int) -> bool:
    """
    Returns True if this is the first view of product_id within the dedup window.
    Uses atomic SET NX so concurrent requests don't double-count.
    Returns False (silently) if Redis is unavailable.
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


def buffer_view_count(product_id: int) -> None:
    """
    Increment the Redis view-count buffer for product_id.
    The flush_view_counts Celery task drains this buffer to the DB in bulk.

    Key:  views:buf:{product_id}
    TTL:  VIEW_BUF_TTL (48 h safety net so orphaned keys don't pile up)
    """
    conn = _get_redis()
    if conn is None:
        # Hard fallback: direct DB update if Redis is down.
        # Slower but never loses the count entirely.
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
        pipe.execute()
    except Exception:
        logger.warning("buffer_view_count: Redis error for product %s", product_id)


# ── Recently viewed ──────────────────────────────────────────────────────────

def update_recently_viewed(request, product_id: int) -> None:
    """
    Prepend product_id to the visitor's recently-viewed list in Redis.
    Deduplicates and caps at RECENTLY_VIEWED_MAX entries.
    Silently no-ops if Redis is unavailable or visitor is unidentified.
    """
    key = _recent_key(request)
    if key is None:
        return
    conn = _get_redis()
    if conn is None:
        return
    try:
        pipe = conn.pipeline()
        pipe.lrem(key, 0, product_id)      # remove any prior occurrence
        pipe.lpush(key, product_id)        # prepend (newest first)
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


# ── GeoIP (unchanged) ───────────────────────────────────────────────────────

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

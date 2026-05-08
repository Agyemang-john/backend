import re
from django.contrib.gis.geoip2 import GeoIP2


def get_region_with_geoip(ip):
    try:
        geo = GeoIP2()
        return geo.city(ip)['region']  # You can use 'country_name' or 'city' too
    except Exception as e:
        print("GeoIP2 error:", e)
        return None


def calculate_packaging_fee(weight, volume):
    # Example rates, adjust as needed
    weight_rate = 1.0  # Packaging fee per kg
    volume_rate = 1.0  # Packaging fee per cubic meter

    weight_fee = weight * weight_rate
    volume_fee = volume * volume_rate

    packaging_fee = weight_fee + volume_fee
    return packaging_fee


from django.conf import settings
from django_redis import get_redis_connection

RECENTLY_VIEWED_MAX = getattr(settings, "RECENTLY_VIEWED_MAX", 10)
VIEW_DEDUP_TTL     = getattr(settings, "VIEW_DEDUP_TTL", 86400)
RECENT_LIST_TTL    = getattr(settings, "RECENT_LIST_TTL", 60 * 60 * 24 * 30)

# Validates that a visitor ID is a standard UUID (prevents injection into Redis keys)
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _get_anonymous_id(request) -> str:
    """
    Stable anonymous identifier for use as Redis key segments.

    Prefers the X-Visitor-ID header (a UUID the browser generates once and
    stores in localStorage + a cookie). This header is sent by BOTH the
    Next.js SSR layer (which forwards it from the browser cookie) and by
    client-side requests, so SSR and browser requests always resolve to the
    same key — preventing double view-counting and keeping the recently-viewed
    list consistent.

    Falls back to the Django session key for any request that doesn't carry
    the header (e.g. direct API calls, old clients).
    """
    visitor_id = request.headers.get('X-Visitor-ID', '')
    if visitor_id and _UUID_RE.match(visitor_id):
        return f"v:{visitor_id}"
    if not request.session.session_key:
        request.session.create()
    return f"s:{request.session.session_key}"


def _dedup_key(request, product_id: int) -> str:
    if request.user.is_authenticated:
        return f"view:user:{request.user.pk}:{product_id}"
    return f"view:anon:{_get_anonymous_id(request)}:{product_id}"


def _recent_key(request) -> str:
    if request.user.is_authenticated:
        return f"recent:user:{request.user.pk}"
    return f"recent:anon:{_get_anonymous_id(request)}"


def update_recently_viewed(request, product_id: int) -> None:
    """
    Prepend product_id to the user's Redis list, removing any earlier
    occurrence first so the list stays deduplicated and ordered by recency.
    """
    conn = get_redis_connection("default")
    key  = _recent_key(request)
    pipe = conn.pipeline()
    pipe.lrem(key, 0, product_id)
    pipe.lpush(key, product_id)
    pipe.ltrim(key, 0, RECENTLY_VIEWED_MAX - 1)
    pipe.expire(key, RECENT_LIST_TTL)
    pipe.execute()


def is_new_view(request, product_id: int) -> bool:
    """
    Returns True (and marks the view) if this is the first time this
    user/session has viewed product_id within the dedup window.
    Atomic SET NX means no double-counting even under concurrent requests.
    """
    conn = get_redis_connection("default")
    key  = _dedup_key(request, product_id)
    return bool(conn.set(key, 1, nx=True, ex=VIEW_DEDUP_TTL))


def get_recently_viewed_ids(request, limit: int = RECENTLY_VIEWED_MAX) -> list[int]:
    """Return ordered list of recently viewed product IDs, newest first."""
    if not request.user.is_authenticated:
        visitor_id = request.headers.get('X-Visitor-ID', '')
        has_visitor_id = bool(visitor_id and _UUID_RE.match(visitor_id))
        if not has_visitor_id and not request.session.session_key:
            return []
    conn = get_redis_connection("default")
    raw  = conn.lrange(_recent_key(request), 0, limit - 1)
    return [int(v) for v in raw]


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
    """Delete the entire recently-viewed list for this user/visitor."""
    conn = get_redis_connection("default")
    conn.delete(_recent_key(request))


def remove_recently_viewed(request, product_id: int) -> None:
    """Remove one product from the recently-viewed list."""
    conn = get_redis_connection("default")
    conn.lrem(_recent_key(request), 0, product_id)

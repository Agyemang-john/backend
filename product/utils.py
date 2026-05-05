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

    # Choose the higher fee or sum both if needed
    # packaging_fee = max(weight_fee, volume_fee)
    packaging_fee = weight_fee + volume_fee
    return packaging_fee

# services.py
from django.conf import settings
from django_redis import get_redis_connection

RECENTLY_VIEWED_MAX = getattr(settings, "RECENTLY_VIEWED_MAX", 10)
VIEW_DEDUP_TTL     = getattr(settings, "VIEW_DEDUP_TTL", 86400)      # 24 h
RECENT_LIST_TTL    = getattr(settings, "RECENT_LIST_TTL", 60 * 60 * 24 * 30)  # 30 days


def _dedup_key(request, product_id: int) -> str:
    if request.user.is_authenticated:
        return f"view:user:{request.user.pk}:{product_id}"
    # Ensure the session exists so session_key is not None
    if not request.session.session_key:
        request.session.create()
    return f"view:anon:{request.session.session_key}:{product_id}"


def _recent_key(request) -> str:
    if request.user.is_authenticated:
        return f"recent:user:{request.user.pk}"
    if not request.session.session_key:
        request.session.create()
    return f"recent:anon:{request.session.session_key}"


def update_recently_viewed(request, product_id: int) -> None:
    """
    Prepend product_id to the user's Redis list, removing any earlier
    occurrence first so the list stays deduplicated and ordered by recency.
    Replaces the old session-based update_recently_viewed(session, product_id).
    """
    conn = get_redis_connection("default")
    key  = _recent_key(request)
    pipe = conn.pipeline()
    pipe.lrem(key, 0, product_id)          # remove existing occurrence (any position)
    pipe.lpush(key, product_id)            # prepend — newest is always index 0
    pipe.ltrim(key, 0, RECENTLY_VIEWED_MAX - 1)
    pipe.expire(key, RECENT_LIST_TTL)
    pipe.execute()


def is_new_view(request, product_id: int) -> bool:
    """
    Returns True (and marks the view) if this is the first time this
    user/session has viewed product_id within the dedup window.
    Atomic SET NX means no double-counting even under concurrent requests
    or React Strict Mode double-mounts.
    Replaces the old session-based viewed_for_count logic.
    """
    conn = get_redis_connection("default")
    key  = _dedup_key(request, product_id)
    return bool(conn.set(key, 1, nx=True, ex=VIEW_DEDUP_TTL))


def get_recently_viewed_ids(request, limit: int = RECENTLY_VIEWED_MAX) -> list[int]:
    """Return ordered list of recently viewed product IDs, newest first."""
    if not request.user.is_authenticated and not request.session.session_key:
        return []
    conn = get_redis_connection("default")
    raw  = conn.lrange(_recent_key(request), 0, limit - 1)
    return [int(v) for v in raw]


def get_recently_viewed_products(request, limit: int = RECENTLY_VIEWED_MAX):
    """
    Return a queryset of Product objects in recency order.
    Replaces the old session-based get_recently_viewed_products() + lru_cache trick.
    """
    from product.models import Product
    from django.db.models import Case, When

    ids = get_recently_viewed_ids(request, limit)
    if not ids:
        return Product.objects.none()

    # Preserve Redis list order in the queryset
    ordering = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(ids)])
    return Product.published.filter(pk__in=ids).order_by(ordering)


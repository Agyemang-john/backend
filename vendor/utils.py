import logging
from django.utils import timezone

from product.utils import (
    _get_visitor_id, detect_bot, detect_device, _get_redis,
    VIEW_DEDUP_TTL, RETURN_WINDOW_TTL, VIEW_BUF_TTL, FLUSH_LOCK_TTL,
)

logger = logging.getLogger(__name__)

_VENDOR_FLUSH_LOCK_KEY = "vendor:views:flush:lock"


def buffer_vendor_view_count(vendor_id: int) -> None:
    conn = _get_redis()
    if conn is None:
        try:
            from django.db.models import F
            from vendor.models import Vendor
            Vendor.objects.filter(id=vendor_id).update(views=F('views') + 1)
        except Exception:
            logger.error("buffer_vendor_view_count: DB fallback failed for vendor %s", vendor_id)
        return
    try:
        pipe = conn.pipeline()
        pipe.incr(f"vendor:views:buf:{vendor_id}")
        pipe.expire(f"vendor:views:buf:{vendor_id}", VIEW_BUF_TTL)
        pipe.set(_VENDOR_FLUSH_LOCK_KEY, 1, nx=True, ex=FLUSH_LOCK_TTL)
        results = pipe.execute()
        flush_needed = bool(results[2])
    except Exception:
        logger.warning("buffer_vendor_view_count: Redis error for vendor %s", vendor_id)
        return

    if flush_needed:
        try:
            from vendor.tasks import flush_vendor_view_counts
            flush_vendor_view_counts.apply_async(countdown=FLUSH_LOCK_TTL)
        except Exception:
            logger.warning("buffer_vendor_view_count: could not schedule flush_vendor_view_counts")


def track_vendor_view(request, vendor_id: int) -> None:
    """
    Single entry point for vendor store view tracking. Non-blocking — all
    analytics writes are async Celery tasks so the store response is never held up.

    Steps:
      1. Dedup: 24h Redis SET NX — skips if same visitor within 24h.
      2. Returning visitor: 30d Redis SET NX.
      3. Bot detection: bots are logged but NOT counted in Vendor.views.
      4. buffer_vendor_view_count(): increments Redis buffer (flushed to DB every 3 min).
      5. log_vendor_view_event.delay(): async task writes one VendorViewLog row.
    """
    visitor_key = _get_visitor_id(request)
    if visitor_key is None:
        return

    conn = _get_redis()
    if conn is None:
        return

    dedup_key = f"vendor:view:dedup:{visitor_key}:{vendor_id}"
    try:
        is_new = bool(conn.set(dedup_key, 1, nx=True, ex=VIEW_DEDUP_TTL))
    except Exception:
        logger.warning("track_vendor_view: dedup Redis error vendor=%s", vendor_id)
        return

    if not is_new:
        return

    try:
        return_key = f"vendor:view:first:{visitor_key}:{vendor_id}"
        first_in_window = bool(conn.set(return_key, 1, nx=True, ex=RETURN_WINDOW_TTL))
        is_returning = not first_in_window
    except Exception:
        is_returning = False

    is_bot = detect_bot(request)
    device = detect_device(request)

    if not is_bot:
        buffer_vendor_view_count(vendor_id)

    user_id = request.user.pk if request.user.is_authenticated else None
    try:
        from vendor.tasks import log_vendor_view_event
        log_vendor_view_event.delay(
            vendor_id=vendor_id,
            visitor_key=visitor_key,
            user_id=user_id,
            is_bot=is_bot,
            is_returning=is_returning,
            device_type=device,
            date_str=timezone.now().date().isoformat(),
        )
    except Exception:
        logger.warning("track_vendor_view: could not enqueue log_vendor_view_event for vendor %s", vendor_id)

from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin
from .models import Vendor
import time


# @user_passes_test(is_vendor)

class SubscriptionCheckMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.user.is_authenticated and getattr(request.user, 'is_vendor', False):
            try:
                vendor = request.user.current_vendor
                if not vendor.has_active_subscription():
                    return redirect('payments:subscribe')  # Redirect to the subscription page
            except Vendor.DoesNotExist:
                pass  # If the user is not a vendor, do nothing


class VendorActivityMiddleware(MiddlewareMixin):
    """
    Intercepts every authenticated vendor API request and stores the current
    timestamp in Redis under `vendor:last_seen:{id}`.

    The flush_vendor_last_seen Celery task drains these keys into the DB every
    5 minutes, so the actual DB write cost is minimal regardless of request rate.

    Skips: unauthenticated requests, non-vendor users, and the heartbeat endpoint
    itself (already handled by the view to avoid double-writes).
    """

    _SKIP_PATHS = {
        '/api/v1/vendor/activity/heartbeat/',
    }

    def process_request(self, request):
        if request.path in self._SKIP_PATHS:
            return None
        if not request.user.is_authenticated:
            return None

        try:
            from django_redis import get_redis_connection
            conn = get_redis_connection("default")

            # Cache user_id → vendor_id in Redis (TTL 24h) to avoid a DB hit
            # on every request. Cache is populated on first miss; "0" marks a
            # user with no store so non-sellers don't query on every request.
            # The id comes from team membership, so activity by any member
            # (owner, admin, staff) keeps the store's last_seen fresh.
            uid_vid_key = f"vendor:uid_vid:{request.user.id}"
            cached = conn.get(uid_vid_key)
            if cached is not None:
                vendor_id = int(cached)
            else:
                from .models import VendorMember
                vendor_id = (
                    VendorMember.objects
                    .filter(user_id=request.user.id, is_active=True, vendor__is_approved=True)
                    .values_list('vendor_id', flat=True)
                    .first()
                ) or 0
                conn.set(uid_vid_key, vendor_id, ex=86400)
            if not vendor_id:
                return None

            conn.set(f"vendor:last_seen:{vendor_id}", int(time.time()), ex=86400)
        except Exception:
            pass  # never block a request due to Redis unavailability
        return None

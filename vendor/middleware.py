from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin
from .models import Vendor
import time


# @user_passes_test(is_vendor)

class SubscriptionCheckMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.user.is_authenticated and request.user.role == 'vendor':
            try:
                vendor = Vendor.objects.get(user=request.user)
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
        if getattr(request.user, 'role', None) != 'vendor':
            return None

        try:
            from django_redis import get_redis_connection
            conn = get_redis_connection("default")

            # Cache user_id → vendor_id in Redis (TTL 24h) to avoid a DB hit
            # on every request. Cache is populated on first miss.
            uid_vid_key = f"vendor:uid_vid:{request.user.id}"
            cached = conn.get(uid_vid_key)
            if cached:
                vendor_id = int(cached)
            else:
                from .models import Vendor
                vendor_id = (
                    Vendor.objects
                    .filter(user_id=request.user.id)
                    .values_list('id', flat=True)
                    .first()
                )
                if not vendor_id:
                    return None
                conn.set(uid_vid_key, vendor_id, ex=86400)

            conn.set(f"vendor:last_seen:{vendor_id}", int(time.time()), ex=86400)
        except Exception:
            pass  # never block a request due to Redis unavailability
        return None

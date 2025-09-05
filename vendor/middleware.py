from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin
from .models import Vendor
from django.contrib.auth.decorators import login_required, user_passes_test
from userauths.utils import is_vendor


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

# Add this middleware to

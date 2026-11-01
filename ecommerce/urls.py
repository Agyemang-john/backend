
from django.contrib import admin
from twofactor import admin_views as twofactor_admin
from twofactor.views import SellerTwoFactorView
from core.health import health_view
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import render

def home(request):
    return render(request, "home.html")


from rest_framework.routers import DefaultRouter
from userauths.djoser_views import RestrictedUserViewSet

_djoser_router = DefaultRouter()
_djoser_router.include_root_view = False
_djoser_router.register("users", RestrictedUserViewSet, basename="user")

urlpatterns = [
    # path('admin/', include('admin_honeypot.urls', namespace='admin_honeypot')),
    path('', home, name='home'),  
    # Before admin.site.urls: staff two-step verification pages (twofactor/admin_views.py).
    path('dashboard/negromart/2fa/', twofactor_admin.verify_view, name='twofactor-admin-verify'),
    path('dashboard/negromart/2fa/setup/', twofactor_admin.setup_view, name='twofactor-admin-setup'),
    path('dashboard/negromart/', admin.site.urls),
    path("api/v1/auth/user/", include("customer.urls")),
    path("api/v1/vendor/", include("vendor.urls")),
    
    path("api/", include(_djoser_router.urls)),   # restricted djoser, see userauths/djoser_views.py
    path("api/", include("userauths.urls")),
    
    path("api/", include("core.urls")),
    path("api/v1/product/", include("product.urls")),
    path("api/v1/payments/", include("payments.urls")),
    path("api/v1/order/", include("order.urls")),
    path("api/v1/address/", include("address.urls")),
    path("api/v1/newsletter/", include("newsletter.urls")),
    path("api/v1/notification/", include("notification.urls")),
    path("api/v1/chat/", include("chat.urls")),
    path("api/v1/recommendations/", include("recommendation.urls")),
    path("api/v1/engagement/", include("engagement.urls")),
    path("api/v1/qa/", include("qa.urls")),
    path("api/v1/attributes/", include("attributes.urls")),
    path("api/v1/tax/", include("tax.urls")),
    path("api/v1/invoices/", include("invoices.urls")),
    path("api/v1/catalog/", include("catalog.urls")),
    path("api/v1/protection/", include("protection.urls")),
    path("api/v1/ads/", include("ads.urls")),
    path("api/v1/risk/", include("risk.urls")),
    path("api/v1/verification/", include("verification.urls")),
    path("api/v1/push/", include("push.urls")),
    path("api/v1/pickup/", include("pickup.urls")),
    path("api/v1/analytics/", include("analytics.urls")),
    path("api/v1/2fa/", SellerTwoFactorView.as_view(), name="seller-2fa"),
    path("api/health/", health_view, name="health"),
    path("ckeditor5/", include('django_ckeditor_5.urls')),
]



if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
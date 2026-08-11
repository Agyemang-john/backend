"""
recommendation/urls.py
Storefront recommendation API, mounted at /api/v1/recommendations/.
"""

from django.urls import path

from .views import (
    CartAddonsAPIView, KeepShoppingAPIView, ModelHealthAPIView, NotInterestedAPIView,
    RecommendedForYouAPIView, TodaysDealsAPIView, TrackEventAPIView, YouMightAlsoLikeAPIView,
)

urlpatterns = [
    path('todays-deals/', TodaysDealsAPIView.as_view(), name='rec-todays-deals'),
    path('for-you/', RecommendedForYouAPIView.as_view(), name='rec-for-you'),
    path('similar/<sku>/<slug>/', YouMightAlsoLikeAPIView.as_view(), name='rec-similar'),
    path('cart-addons/', CartAddonsAPIView.as_view(), name='rec-cart-addons'),
    path('keep-shopping/', KeepShoppingAPIView.as_view(), name='rec-keep-shopping'),
    path('track/', TrackEventAPIView.as_view(), name='rec-track'),
    path('not-interested/', NotInterestedAPIView.as_view(), name='rec-not-interested'),
    path('health/', ModelHealthAPIView.as_view(), name='rec-health'),
]

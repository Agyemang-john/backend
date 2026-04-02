"""
core/urls.py
URL routing for the core app (homepage data, navigation, recommendations).
"""

from django.urls import path
from .views import *

urlpatterns = [
    # Temporary debug endpoint — remove after confirming IP detection works
    path('debug/ip/', DebugIPView.as_view(), name='debug-ip'),
    path('sliders/', HomeSliderView.as_view(), name='home-sliders'),
    path('banners/', BannersView.as_view(), name='home-banners'),
    path('menu-categories/', MainCategoryWithCategoriesAPIView.as_view(), name='menu-categories'),
    path('top-category/', TopEngagedCategoryView.as_view(), name='top-category'),
    path('category/<slug:slug>/', CategoryDetailView.as_view(), name='category-detail'),
    path('index/', MainAPIView.as_view(), name='index'),
    path('recently-related/', RecentlyViewedRelatedProductsAPIView.as_view(), name='recently-related'),
    path('searched-products/', SearchedProducts.as_view(), name='searched-products'),
    path('recommended-products/', RecommendedProducts.as_view(), name='recommended-products'),
    path('trending-products/', TrendingProductsAPIView.as_view(), name='trending-products'),
    path('cart-suggested-products/', SuggestedCartProductsAPIView.as_view(), name='cart-suggested-products'),
]

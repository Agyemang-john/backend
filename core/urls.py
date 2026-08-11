"""
core/urls.py
URL routing for the core app (homepage data, navigation, promos).

Recommendation and deals endpoints live in the `recommendation` app, mounted
at /api/v1/recommendations/ — the rails that used to be served from here are
now driven by the trained models rather than by ad-hoc queries.
"""

from django.urls import path
from .views import *

urlpatterns = [
    # Temporary debug endpoint — remove after confirming IP detection works
    path('debug/ip/', DebugIPView.as_view(), name='debug-ip'),
    path('sliders/', HomeSliderView.as_view(), name='home-sliders'),
    path('banners/', BannersView.as_view(), name='home-banners'),
    path('promo-grid/', PromoGridView.as_view(), name='promo-grid'),
    path('menu-categories/', MainCategoryWithCategoriesAPIView.as_view(), name='menu-categories'),
    path('top-category/', TopEngagedCategoryView.as_view(), name='top-category'),
    path('category/<slug:slug>/', CategoryDetailView.as_view(), name='category-detail'),
    path('index/', MainAPIView.as_view(), name='index'),
    path('searched-products/', SearchedProducts.as_view(), name='searched-products'),
    path('search-history/', SearchHistoryView.as_view(), name='search-history'),
    path('community/links/', SocialLinksView.as_view(), name='community-links'),
]

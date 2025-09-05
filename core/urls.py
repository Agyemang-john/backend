from django.urls import path
from .views import *

urlpatterns = [
    path('menu-categories/', MainCategoryWithCategoriesAPIView.as_view(), name='menu-categories'),
    path('top-category/', TopEngagedCategoryView.as_view(), name='top-category'),
    path('category/<slug:slug>/', CategoryDetailView.as_view(), name='category-detail'),
    path('index/', MainAPIView.as_view(), name='index'),
    path('recently-related/', RecentlyViewedRelatedProductsAPIView.as_view(), name='viewed-products-based-id-2'),
    path('searched-products/', SearchedProducts.as_view(), name='searched-products'),
    path('recommended-products/', RecommendedProducts.as_view(), name='recommended-products'),
    path('trending-products/', TrendingProductsAPIView.as_view(), name='trending-products'),
    path('cart-suggested-products/', SuggestedCartProductsAPIView.as_view(), name='cart-suggested-products'),

    # ###############################################CATEGORY
    # path('subcategory/<slug:slug>/', SubcategoryListView.as_view(), name='sub-category'),
    # ###############################################CATEGORY

]

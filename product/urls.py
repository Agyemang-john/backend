
from django.urls import path
from . import views 

urlpatterns = [
    # AJAX and custom endpoints
    path('add-review/', views.AddProductReviewView.as_view(), name='product-review-create'),
    path('sitemap-data/', views.SitemapDataAPIView.as_view(), name='sitemap-data'),
    path('ajaxcolor/', views.AjaxColorAPIView.as_view(), name='change_color'),

    # Category and brand list views (general slug-based)
    path('category/<slug>/', views.CategoryProductListView.as_view(), name='category'),
    path('brand/<slug>/', views.BrandProductListView.as_view(), name='brand'),
    path('search/', views.ProductSearchAPIView.as_view(), name='product-search'),
    path('search-suggestions/', views.SearchSuggestionsAPIView.as_view(), name='search-suggestions'),

    # Detailed product-related views
    path('<sku>/<slug>/', views.ProductDetailAPIView.as_view(), name='product-detail-api'),
    # path('cart/<sku>/<slug>/', CartDataView.as_view(), name='cart-data'),

    # Utility or miscellaneous views
    path('recently-viewed-products/', views.RecentlyViewedProducts.as_view(), name='recently-viewed'),
    path('recommendations/', views.CartRecommendationsAPIView.as_view(), name='cart-recommendations'),
    path('frequently-bought/', views.FrequentlyBoughtTogetherAPIView.as_view(), name='fbt'),
]
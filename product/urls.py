
from django.urls import path
from .views import AjaxColorAPIView, ProductDetailAPIView, CategoryProductListView, BrandProductListView, CartDataView, RecentlyViewedProducts, ProductSearchAPIView, SearchSuggestionsAPIView, CartRecommendationsAPIView, FrequentlyBoughtTogetherAPIView, ProductsAPIView

urlpatterns = [
    # AJAX and custom endpoints
    path('products/', ProductsAPIView.as_view(), name='products'),
    path('ajaxcolor/', AjaxColorAPIView.as_view(), name='change_color'),

    # Category and brand list views (general slug-based)
    path('category/<slug>/', CategoryProductListView.as_view(), name='category'),
    path('brand/<slug>/', BrandProductListView.as_view(), name='brand'),
    path('search/', ProductSearchAPIView.as_view(), name='product-search'),
    path('search-suggestions/', SearchSuggestionsAPIView.as_view(), name='search-suggestions'),

    # Detailed product-related views
    path('<sku>/<slug>/', ProductDetailAPIView.as_view(), name='product-detail-api'),
    path('cart/<sku>/<slug>/', CartDataView.as_view(), name='cart-data'),

    # Utility or miscellaneous views
    path('recently-viewed-products/', RecentlyViewedProducts.as_view(), name='recently-viewed'),
    path('recommendations/', CartRecommendationsAPIView.as_view(), name='cart-recommendations'),
    path('frequently-bought/', FrequentlyBoughtTogetherAPIView.as_view(), name='fbt'),
]
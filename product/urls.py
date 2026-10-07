
from django.urls import path
from . import views
from . import review_views

urlpatterns = [
    path('flash-sales/', views.FlashSaleListAPIView.as_view(), name='flash-sales'),
    path('occasions/', views.OccasionListAPIView.as_view(), name='occasions'),
    path('collection/<slug:slug>/', views.CollectionAPIView.as_view(), name='collection'),

    # AJAX and custom endpoints
    path('add-review/', views.AddProductReviewView.as_view(), name='product-review-create'),

    # Reviews (product/review_views.py). Fixed paths before <int:...> ones.
    path('<int:product_id>/reviews/', review_views.ProductReviewListView.as_view(), name='product-reviews'),
    path('<int:product_id>/reviews/summary/', review_views.ProductReviewSummaryView.as_view(), name='product-review-summary'),
    path('<int:product_id>/reviews/eligibility/', review_views.ReviewEligibilityView.as_view(), name='product-review-eligibility'),
    path('reviews/mine/', review_views.MyReviewsView.as_view(), name='my-reviews'),
    path('reviews/moderation/', review_views.ModerationQueueView.as_view(), name='review-moderation-queue'),
    path('reviews/media/', review_views.ReviewMediaUploadView.as_view(), name='review-media-upload'),
    path('reviews/media/<int:media_id>/', review_views.ReviewMediaDeleteView.as_view(), name='review-media-delete'),
    path('reviews/<int:review_id>/', review_views.ReviewDetailView.as_view(), name='review-detail'),
    path('reviews/<int:review_id>/helpful/', review_views.ReviewHelpfulView.as_view(), name='review-helpful'),
    path('reviews/<int:review_id>/moderate/', review_views.ModerateReviewView.as_view(), name='review-moderate'),
    path('reviews/<int:review_id>/history/', review_views.ReviewHistoryView.as_view(), name='review-history'),
    path('sitemap-data/', views.SitemapDataAPIView.as_view(), name='sitemap-data'),
    # path('ajaxcolor/', views.AjaxColorAPIView.as_view(), name='change_color'),

    # Category and brand list views (general slug-based)
    path('category/<slug>/', views.CategoryProductListView.as_view(), name='category'),
    path('brand/<slug>/', views.BrandProductListView.as_view(), name='brand'),
    path('search/', views.ProductSearchAPIView.as_view(), name='product-search'),
    path('search-suggestions/', views.SearchSuggestionsAPIView.as_view(), name='search-suggestions'),

    # Detailed product-related views
    # path('cart/<sku>/<slug>/', CartDataView.as_view(), name='cart-data'),

    # Utility or miscellaneous views
    path('recently-viewed/clear/', views.ClearRecentlyViewed.as_view(), name='clear'),
    path('recently-viewed/remove/', views.RemoveRecentlyViewedItem.as_view(), name='remove-item'),
    path('recently-viewed/sync/', views.SyncRecentlyViewedView.as_view(), name='sync-recently-viewed'),
    path('recently-viewed-products/', views.RecentlyViewedProducts.as_view(), name='viewed'),

    path('mark-viewed/', views.MarkProductViewedAPIView.as_view(), name='mark-viewed'),

    # Product detail view (specific SKU and slug)
    path('<sku>/<slug>/auth-state/', views.ProductAuthStateAPIView.as_view(), name='product-auth-state'),
    path('<sku>/<slug>/', views.ProductDetailAPIView.as_view(), name='product-detail-api'),
]
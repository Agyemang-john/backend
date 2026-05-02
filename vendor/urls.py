from django.urls import path
from .views import (
    VendorAccountAPIView,
    VendorDeletionRequestView,
    VendorStatusAPIView,
    VendorSignupAPIView,
    VendorDetailView,
    VendorProductsView,
    VendorReviewsView,
    VendorOrderDetailView,
    UpdateOrderStatusAPIView,
    ProductRelatedDataAPIView,
    ProductListCreateView,
    ProductCreateView,
    ProductDetailView,
    VendorPaymentMethodAPIView,  # Updated to APIView
    OpeningHourAPIView,
    AboutManagementAPIView,
    VendorProductReviewsAPIView,
    VendorOrderListAPIView,
    # VendorOrderStatusUpdateAPIView,
    SalesSummaryView,
    SalesTrendView,
    TopProductsView,
    OrderStatusView,
    EngagementView,
    DeliveryPerformanceView,
    LocationAutocompleteView,
    BankValidationView,
    PayoutListView,
    ProductAnalyticsDetailView,
    CheckCustomerAuth,
    ShipmentAPIView,
    TrackingEventAddAPIView,
)

from vendor.bulk_upload_views import (
    BulkProductUploadAPIView,
    BulkUploadTemplatAPIView,
    BulkUploadMetaAPIView,
)
 

urlpatterns = [
    # Vendor Routes
    path('location/autocomplete/', LocationAutocompleteView.as_view(), name='location-autocomplete'),
    path('check/', CheckCustomerAuth.as_view(), name='check-auth'),
    path('my-status/', VendorStatusAPIView.as_view(), name='vendor-my-status'),
    path('register/', VendorSignupAPIView.as_view(), name='vendor-register'),
    path('product-related-data/', ProductRelatedDataAPIView.as_view(), name='product-related-data'),

    # Product Routes
    path('products/', ProductListCreateView.as_view(), name='product-list-create'),
    path('products/create/', ProductCreateView.as_view(), name='product-create'),
    path('products/<int:pk>/', ProductDetailView.as_view(), name='product-detail'),
    # Payment Method Route
    path('payment-method/', VendorPaymentMethodAPIView.as_view(), name='vendor-payment-method'),
    path('validate-bank/', BankValidationView.as_view(), name='validate-bank'),
    path('payouts/', PayoutListView.as_view(), name='payout-list'),

    # Opening Hours Route
    path('opening-hours/', OpeningHourAPIView.as_view(), name='opening-hours-list'),
    path('opening-hours/<int:pk>/', OpeningHourAPIView.as_view(), name='opening-hours-detail'),

    # About Route
    path('about/management/', AboutManagementAPIView.as_view(), name='about-detail'),
    # Vendor account (business details + documents)
    path('account/', VendorAccountAPIView.as_view(), name='vendor-account'),
    path('deletion-request/', VendorDeletionRequestView.as_view(), name='vendor-deletion-request'),

    path("reviews/", VendorProductReviewsAPIView.as_view(), name="vendor-reviews"),
    path("reviews/<int:pk>/", VendorProductReviewsAPIView.as_view(), name="vendor-review-update"),

    path('orders/', VendorOrderListAPIView.as_view(), name='vendor-order-list'),
    path('orders/<int:id>/detail/', VendorOrderDetailView.as_view(), name='vendor-order-detail'),
    path('orders/<int:id>/status/', UpdateOrderStatusAPIView.as_view(), name='vendor-order-status-update'),
    path('orders/<int:id>/shipment/', ShipmentAPIView.as_view(), name='vendor-shipment-list-create'),
    path('orders/<int:id>/shipment/<str:shipment_id>/', ShipmentAPIView.as_view(), name='vendor-shipment-update'),
    path('orders/<int:id>/shipment/<str:shipment_id>/event/', TrackingEventAddAPIView.as_view(), name='vendor-tracking-event'),

    # Ananlitics Views
    path('sales-summary/', SalesSummaryView.as_view(), name='sales-summary'),
    path('sales-trend/', SalesTrendView.as_view(), name='sales-trend'),
    path('top-products/', TopProductsView.as_view(), name='top-products'),
    path('order-status/', OrderStatusView.as_view(), name='order-status'),
    path('engagement/', EngagementView.as_view(), name='engagement'),
    path('delivery-performance/', DeliveryPerformanceView.as_view(), name='delivery-performance'),

    path('seller-detail/<slug:slug>/', VendorDetailView.as_view(), name='vendor-detail'),
    path('seller-detail/<slug:slug>/products/', VendorProductsView.as_view(), name='vendor-products'),
    path('seller-detail/<slug:slug>/reviews/', VendorReviewsView.as_view(), name='vendor-reviews'),

    path('products/<int:pk>/analytics/', ProductAnalyticsDetailView.as_view(), name='product-analytics-detail'),

    path(
        "products/bulk-upload/",
        BulkProductUploadAPIView.as_view(),
        name="bulk-product-upload",
    ),
    path(
        "products/bulk-upload/template/",
        BulkUploadTemplatAPIView.as_view(),
        name="bulk-upload-template",
    ),
    path(
        "products/bulk-upload/meta/",
        BulkUploadMetaAPIView.as_view(),
        name="bulk-upload-meta",
    ),
]

# Note: Removed router.urls since payment-method now uses APIView
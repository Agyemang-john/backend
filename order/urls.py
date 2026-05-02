from django.urls import path
from . import views
from . import admin_views

urlpatterns = [
    # ── Customer tracking ──────────────────────────────────────────────────
    path('tracking/<int:order_id>/', views.OrderTrackingAPIView.as_view(), name='order-tracking'),

    # ── Admin tracking management ──────────────────────────────────────────
    path('admin/dashboard/', admin_views.AdminTrackingDashboardView.as_view(), name='admin-tracking-dashboard'),
    path('admin/orders/', admin_views.AdminOrderListView.as_view(), name='admin-order-list'),
    path('admin/orders/<int:order_id>/', admin_views.AdminOrderDetailView.as_view(), name='admin-order-detail'),
    path('admin/orders/<int:order_id>/status/', admin_views.AdminOrderStatusView.as_view(), name='admin-order-status'),
    path('admin/orders/<int:order_id>/broadcast/', admin_views.AdminBroadcastView.as_view(), name='admin-order-broadcast'),
    path('admin/orders/<int:order_id>/shipment/', admin_views.AdminShipmentView.as_view(), name='admin-shipment-list-create'),
    path('admin/orders/<int:order_id>/shipment/<str:shipment_id>/', admin_views.AdminShipmentView.as_view(), name='admin-shipment-update'),
    path('admin/orders/<int:order_id>/shipment/<str:shipment_id>/event/', admin_views.AdminTrackingEventView.as_view(), name='admin-tracking-event'),

    path('add-to-cart/', views.AddToCartView.as_view(), name='add-to-cart'),
    path('remove-cart/', views.RemoveFromCartView.as_view(), name='remove-cart'),
    path('sync-guest-cart/', views.SyncGuestCartView.as_view(), name='sync-cart'),
    path('quantity/', views.CartQuantityView.as_view(), name='quantity'),
    path('cart/', views.CartView.as_view(), name='cart'),
    path('info/', views.NavInfo.as_view(), name='info'),
    path('checkout/', views.CheckoutAPIView.as_view(), name='checkout'),
    path('update-delivery/', views.UpdateDeliveryOptionAPIView.as_view(), name='update-delivery'),
    path('summary/', views.CartSummaryAPIView.as_view(), name='summary'),
    path('address/default/', views.DefaultAddressAPIView.as_view(), name='default-address'),
    path('receipt/<int:order_id>/', views.OrderReceiptAPIView.as_view(), name='order-receipt'),
]
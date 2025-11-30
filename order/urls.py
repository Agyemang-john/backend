from django.urls import path
from . import views

urlpatterns = [
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
from django.urls import path

from . import views

urlpatterns = [
    path('orders/<int:order_id>/receipt/', views.OrderReceiptView.as_view(), name='order-receipt'),
    path('seller/orders/<int:order_id>/', views.SellerOrderInvoiceView.as_view(), name='seller-order-invoice'),
]

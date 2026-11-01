from django.urls import path

from . import views

urlpatterns = [
    path('seller/orders/<int:order_id>/hold/', views.SellerOrderHoldView.as_view(), name='risk-seller-hold'),
]

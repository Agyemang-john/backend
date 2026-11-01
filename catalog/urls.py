from django.urls import path

from . import views

urlpatterns = [
    path('products/<int:pk>/offers/', views.ProductOffersView.as_view(), name='catalog-product-offers'),
    path('seller/offers/', views.SellerOfferView.as_view(), name='catalog-seller-offers'),
]

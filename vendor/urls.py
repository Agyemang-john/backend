from django.urls import include, path
from userauths import views as account_views
from . import views


from .views import *

urlpatterns = [
 ############################  VENDOR  ####################
   
    path('register/', VendorSignUpView.as_view(), name='vendor-register'),
    path('seller-detail/<slug>/', VendorDetailView.as_view(), name='vendor-detail'),
    path('data/', VendorAPIView.as_view(), name='vendor'),
    path('opening-hours/', OpeningHourDetailView.as_view(), name='opening-hour'),
    path('order/<int:id>/', OrderDetailsAPIView.as_view(), name='order-details'),
    path('opening-hours/<int:pk>/', OpeningHourDetailView.as_view(), name='opening-hour-detail'),
    path('order-status-change/<int:order_id>/', UpdateOrderStatusAPIView.as_view(), name='change-order-status'),
    path('order/<int:id>/update-status/', UpdateOrderProductStatusAPIView.as_view(), name='update-order-product-status'),
    path('about/', AboutAPIView.as_view(), name='about-list-create'),
    path('product-related-data/', ProductRelatedDataAPIView.as_view(), name='product-related-data'),
    path('product/add/', VendorProductAPIView.as_view(), name='add-product'),
    path('product/change/<int:product_id>/', VendorProductAPIView.as_view(), name='edit-product'),
    path('products/', VendorProducts.as_view(), name='products'),
    path('payment-method/', VendorPaymentMethodAPIView.as_view(), name='vendor-payment-method'),

]
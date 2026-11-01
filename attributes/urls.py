from django.urls import path

from . import views

urlpatterns = [
    path('sub-category/<int:pk>/', views.SubCategoryAttributesView.as_view(), name='attributes-sub-category'),
    path('seller/products/<int:pk>/', views.SellerProductAttributesView.as_view(), name='attributes-seller-product'),
    path('compare/', views.CompareView.as_view(), name='attributes-compare'),
]

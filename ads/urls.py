from django.urls import path

from . import views

urlpatterns = [
    path('click/', views.ClickView.as_view(), name='ads-click'),
    path('seller/campaigns/', views.SellerCampaignsView.as_view(), name='ads-seller-campaigns'),
    path('seller/campaigns/<int:pk>/', views.SellerCampaignView.as_view(), name='ads-seller-campaign'),
]

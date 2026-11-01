from django.urls import path, re_path

from . import views

urlpatterns = [
    path('lines/<int:line_id>/eligibility/', views.EligibilityView.as_view(), name='claim-eligibility'),
    path('claims/', views.MyClaimsView.as_view(), name='my-claims'),
    path('claims/<str:ref>/', views.MyClaimView.as_view(), name='my-claim'),
    re_path(r'^claims/(?P<ref>[A-Z0-9-]+)/(?P<action>messages|accept|escalate|withdraw)/$', views.MyClaimView.as_view(),
            name='my-claim-action'),
    path('seller/claims/', views.SellerClaimsView.as_view(), name='seller-claims'),
    path('seller/claims/<str:ref>/', views.SellerClaimView.as_view(), name='seller-claim'),
    re_path(r'^seller/claims/(?P<ref>[A-Z0-9-]+)/(?P<action>messages|offer)/$', views.SellerClaimView.as_view(),
            name='seller-claim-action'),
]

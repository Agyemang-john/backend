from django.urls import path

from . import views

urlpatterns = [
    path('seller/', views.SellerVerificationView.as_view(), name='seller-verification'),
]

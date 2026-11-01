from django.urls import path

from . import views

urlpatterns = [
    path('seller/registration/', views.SellerTaxRegistrationView.as_view(), name='tax-seller-registration'),
]

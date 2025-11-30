from django.urls import path
from . import views
from .flutterwave_view import FlutterwaveCallbackAPIView

app_name = 'payments'

urlpatterns = [
    path('verify-payment/<str:reference>/', views.VerifyPaymentAPIView.as_view(), name='verify_payment'),
    path('flutterwave-callback/', FlutterwaveCallbackAPIView.as_view(), name='flutterwave-callback'),
]
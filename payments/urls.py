from django.urls import path
from . import views
from .flutterwave_view import FlutterwaveCallbackAPIView

app_name = 'payments'

urlpatterns = [
    path('create-payment/', views.create_payment, name='create_payment'),
    path('execute-payment/', views.execute_payment, name='execute_payment'),
    path('payment-cancelled/', views.payment_cancelled, name='payment_cancelled'),

    path('subscribe/', views.subscribe, name='subscribe'),
    path('s/verify/', views.subscription_verify_payment, name='verify'),
    path('s/execute/', views.paystack_execute_subscription, name='paystack_execute_subscription'),

    ###########################################
    path('c/check/', views.paystack_create_payment, name='paystack_pay'),
    path('c/execute/', views.paystack_execute, name='paystack_execute'),
    ###########################################
    path('f/initiate/', views.execute_flutter, name='flutter_execute'),
    path('f/execute/', views.flutter_payment, name='flutter_pay'),


    path('verify-payment/<str:reference>/', views.VerifyPaymentAPIView.as_view(), name='verify_payment'),

    path('flutterwave-callback/', FlutterwaveCallbackAPIView.as_view(), name='flutterwave-callback'),
]
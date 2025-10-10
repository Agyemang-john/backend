from django.urls import path, re_path
from .views import (
    CustomTokenObtainPairView,
    CustomVendorTokenObtainPairView,
    CustomVendorTokenRefreshView,
    CustomTokenRefreshView,
    CustomTokenVerifyView,
    LogoutView,
    OTPVerifyView,
    OTPResendView
)


urlpatterns = [
    path('jwt/create/', CustomTokenObtainPairView.as_view()),
    path('jwt/refresh/', CustomTokenRefreshView.as_view()),
    path('jwt/create/vendor/', CustomVendorTokenObtainPairView.as_view()),
    path('jwt/refresh/vendor/', CustomVendorTokenRefreshView.as_view()),
    path('jwt/verify/', CustomTokenVerifyView.as_view()),
    path('logout/', LogoutView.as_view()),
    path('jwt/otp-verify/', OTPVerifyView.as_view(), name='otp_verify'),
    path('jwt/otp-resend/', OTPResendView.as_view(), name='otp_verify'),
]
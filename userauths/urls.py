from django.urls import path, re_path
from .views import (
    CustomTokenObtainPairView,
    CustomTokenRefreshView,
    CustomTokenVerifyView,
    LogoutView,
    #  Vendor Auth Views
    VendorTokenRefreshView,
    VendorTokenObtainPairView,
    VendorOTPVerifyView,
    VendorOTPResendView,
    VendorLogoutView,
    VendorTokenVerifyView,
)


urlpatterns = [
    path('jwt/create/', CustomTokenObtainPairView.as_view()),
    path('jwt/refresh/', CustomTokenRefreshView.as_view()),
    path('jwt/verify/', CustomTokenVerifyView.as_view()),
    path('logout/', LogoutView.as_view()),


    # Vendor Auth URLs
    path('jwt/create/vendor/', VendorTokenObtainPairView.as_view()),
    path('jwt/refresh/vendor/', VendorTokenRefreshView.as_view()),
    path('jwt/verify/vendor/', VendorTokenVerifyView.as_view()),

    path('jwt/otp-verify/vendor/', VendorOTPVerifyView.as_view(), name='otp_verify'),
    path('jwt/otp-resend/vendor/', VendorOTPResendView.as_view(), name='otp_verify'),
    path('vendor/logout/', VendorLogoutView.as_view()),
]
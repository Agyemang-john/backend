from django.urls import path
from .views import (
    CustomTokenObtainPairView,
    CustomTokenRefreshView,
    CustomTokenVerifyView,
    LogoutView,
    # Vendor Auth Views
    VendorTokenRefreshView,
    VendorTokenObtainPairView,
    VendorOTPVerifyView,
    VendorOTPResendView,
    VendorLogoutView,
    VendorTokenVerifyView,
    RegisterView,
    ActivateEmailView,
    # Session management — vendor
    VendorSessionListView,
    VendorSessionRevokeView,
    VendorLogoutAllView,
    VendorLogoutOtherSessionsView,
    # Session management — customer
    CustomerSessionListView,
    CustomerSessionRevokeView,
    CustomerLogoutAllView,
)


urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path('activate/', ActivateEmailView.as_view(), name='activate'),

    path('jwt/create/', CustomTokenObtainPairView.as_view()),
    path('jwt/refresh/', CustomTokenRefreshView.as_view()),
    path('jwt/verify/', CustomTokenVerifyView.as_view()),
    path('logout/', LogoutView.as_view()),

    # Customer session management
    path('sessions/', CustomerSessionListView.as_view(), name='customer-sessions'),
    path('sessions/<uuid:session_id>/', CustomerSessionRevokeView.as_view(), name='customer-session-revoke'),
    path('logout-all/', CustomerLogoutAllView.as_view(), name='customer-logout-all'),

    # Vendor Auth URLs
    path('jwt/create/vendor/', VendorTokenObtainPairView.as_view()),
    path('jwt/refresh/vendor/', VendorTokenRefreshView.as_view()),
    path('jwt/verify/vendor/', VendorTokenVerifyView.as_view()),

    path('jwt/otp-verify/vendor/', VendorOTPVerifyView.as_view(), name='otp_verify'),
    path('jwt/otp-resend/vendor/', VendorOTPResendView.as_view(), name='otp_resend'),
    path('vendor/logout/', VendorLogoutView.as_view()),

    # Vendor session management
    path('vendor/sessions/', VendorSessionListView.as_view(), name='vendor-sessions'),
    path('vendor/sessions/<uuid:session_id>/', VendorSessionRevokeView.as_view(), name='vendor-session-revoke'),
    path('vendor/logout-all/', VendorLogoutAllView.as_view(), name='vendor-logout-all'),
    path('vendor/logout-other-sessions/', VendorLogoutOtherSessionsView.as_view(), name='vendor-logout-other'),
]

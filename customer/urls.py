from django.urls import path

from .views import ProfileAPIView, UserOrdersView, OrderDetailView, UserReviewsAPIView, ChangePasswordView


urlpatterns = [
    path('profile/', ProfileAPIView.as_view(), name='profile'),
    path('orders/', UserOrdersView.as_view(), name='user-orders'),
    path('order/<int:id>/', OrderDetailView.as_view(), name='order-detail'),
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('reviews/', UserReviewsAPIView.as_view(), name='reviews'),
]
from django.urls import path

from . import views

urlpatterns = [
    path('profile/', views.ProfileAPIView.as_view(), name='profile'),
    path('orders/', views.UserOrdersView.as_view(), name='user-orders'),
    path('order/<int:id>/', views.OrderDetailView.as_view(), name='order-detail'),
    path('change-password/', views.ChangePasswordView.as_view(), name='change-password'),
    path('reviews/', views.UserReviewsAPIView.as_view(), name='reviews'),
]
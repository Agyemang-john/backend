from django.urls import path

from . import views

urlpatterns = [
    path('alerts/', views.AlertListCreateView.as_view(), name='product-alerts'),
    path('alerts/<int:pk>/', views.AlertDetailView.as_view(), name='product-alert-detail'),
    path('email-preferences/', views.EmailPreferencesView.as_view(), name='email-preferences'),
    path('unsubscribe/<str:token>/', views.UnsubscribeView.as_view(), name='email-unsubscribe'),
]

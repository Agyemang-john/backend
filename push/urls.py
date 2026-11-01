from django.urls import path

from . import views

urlpatterns = [
    path('config/', views.PushConfigView.as_view(), name='push-config'),
    path('subscriptions/', views.SubscriptionView.as_view(), name='push-subscriptions'),
]

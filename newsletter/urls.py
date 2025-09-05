# apps/newsletters/urls.py
from django.urls import path
from .views import SubscribeAPIView, ConfirmAPIView, UnsubscribeAPIView

urlpatterns = [
    path("subscribe/", SubscribeAPIView.as_view(), name="subscribe"),
    path("confirm/<str:token>/", ConfirmAPIView.as_view(), name="confirm"),
    path("unsubscribe/<str:token>/", UnsubscribeAPIView.as_view(), name="unsubscribe"),
]

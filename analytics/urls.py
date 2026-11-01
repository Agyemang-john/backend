from django.urls import path

from . import views

urlpatterns = [
    path('collect/', views.CollectView.as_view(), name='analytics-collect'),
    path('experiments/', views.ExperimentsView.as_view(), name='analytics-experiments'),
]

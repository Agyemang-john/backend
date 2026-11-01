from django.urls import path

from . import views

urlpatterns = [
    path('points/', views.PointsView.as_view(), name='pickup-points'),
    path('select/', views.SelectPointView.as_view(), name='pickup-select'),
    path('orders/<int:order_id>/', views.OrderParcelView.as_view(), name='pickup-order-parcel'),
    path('station/parcels/', views.StationParcelsView.as_view(), name='pickup-station-parcels'),
    path('station/collect/', views.StationCollectView.as_view(), name='pickup-station-collect'),
]

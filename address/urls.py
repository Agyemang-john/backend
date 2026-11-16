from django.urls import path

from .views import (
    AddressDetailView, AddressFormatView, AddressListCreateView, GeoPlaceView, GeoReverseView, GeoSearchView,
    MakeDefaultAddressView,
)

urlpatterns = [
    path('addresses/', AddressListCreateView.as_view(), name='address-list-create'),
    path('addresses/default/', MakeDefaultAddressView.as_view(), name='make-default-address'),
    path('addresses/<int:id>/', AddressDetailView.as_view(), name='address-detail'),
    path('search/', GeoSearchView.as_view(), name='address-search'),
    path('place/', GeoPlaceView.as_view(), name='address-place'),
    path('reverse/', GeoReverseView.as_view(), name='address-reverse'),
    path('formats/<str:code>/', AddressFormatView.as_view(), name='address-format'),
]

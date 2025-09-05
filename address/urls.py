from django.urls import path

from .views import *

urlpatterns = [
    path('addresses/', AddressListCreateView.as_view(), name='address-list-create'),
    path('addresses/<int:id>/', AddressDetailView.as_view(), name='address-detail'),
    path('addresses/default/', MakeDefaultAddressView.as_view(), name='make-default-address'),
]
from rest_framework import serializers
from .models import *


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = ['id', 'user','latitude','longitude','full_name', 'country', 'region', 'town', 'address', 'gps_address', 'email', 'mobile', 'status','date_added']
        read_only_fields = ['id', 'date_added']
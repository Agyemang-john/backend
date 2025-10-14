# address/serializers.py
from rest_framework import serializers
from address.models import Address
import re

class AddressSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(
        error_messages={
            'required': 'Full name is required.',
            'blank': 'Full name cannot be empty.',
            'max_length': 'Full name cannot exceed 50 characters.'
        }
    )
    country = serializers.CharField(
        required=False,
        allow_blank=True,
        error_messages={
            'max_length': 'Country name cannot exceed 20 characters.'
        }
    )
    region = serializers.CharField(
        required=False,
        allow_blank=True,
        error_messages={
            'max_length': 'Region cannot exceed 30 characters.'
        }
    )
    town = serializers.CharField(
        required=False,
        allow_blank=True,
        error_messages={
            'max_length': 'Town cannot exceed 30 characters.'
        }
    )
    address = serializers.CharField(
        error_messages={
            'required': 'Address line is required.',
            'blank': 'Address line cannot be empty.',
            'max_length': 'Address cannot exceed 300 characters.'
        }
    )
    gps_address = serializers.CharField(
        required=False,
        allow_blank=True,
        error_messages={
            'max_length': 'GPS address cannot exceed 19 characters.'
        }
    )
    email = serializers.EmailField(
        required=False,
        allow_blank=True,
        error_messages={
            'invalid': 'Enter a valid email address.'
        }
    )
    mobile = serializers.CharField(
        required=False,
        allow_blank=True,
        error_messages={
            'invalid': 'Enter a valid mobile number (e.g., +1234567890).',
            'max_length': 'Mobile number cannot exceed 15 characters.'
        }
    )
    status = serializers.BooleanField(
        default=False,
        error_messages={
            'invalid': 'Status must be a boolean value.'
        }
    )

    class Meta:
        model = Address
        fields = ['id', 'user', 'latitude', 'longitude', 'full_name', 'country', 'region', 'town', 'address', 'gps_address', 'email', 'mobile', 'status', 'date_added']
        read_only_fields = ['id', 'user', 'date_added']

    def validate_mobile(self, value):
        if value and not re.match(r'^\+?\d{10,15}$', value):
            raise serializers.ValidationError('Enter a valid mobile number (e.g., +1234567890).')
        return value

    def validate(self, data):
        # Ensure at least one of mobile or email is provided
        if not data.get('mobile') and not data.get('email'):
            raise serializers.ValidationError({
                'non_field_errors': ['At least one of mobile or email must be provided.']
            })
        # Validate country against a list of valid country names (optional)
        if data.get('country'):
            from pycountry import countries
            country_names = {country.name for country in countries}
            if data['country'] not in country_names:
                raise serializers.ValidationError({
                    'country': ['Invalid country name. Please select a valid country.']
                })
        return data
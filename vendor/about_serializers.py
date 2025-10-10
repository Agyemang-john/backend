from rest_framework import serializers
from .models import Vendor, About
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Q
from datetime import time

class AboutSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source='vendor.name', required=True)
    profile_image = serializers.ImageField(required=False, allow_null=True)
    cover_image = serializers.ImageField(required=False, allow_null=True)

    class Meta:
        model = About
        fields = [
            'vendor_name', 'address', 'about', 'profile_image', 'cover_image', 
            'latitude', 'longitude', 'shipping_on_time', 'chat_resp_time', 
            'authentic_rating', 'day_return', 'waranty_period', 
            'facebook_url', 'instagram_url', 'twitter_url', 'linkedin_url'
        ]
        read_only_fields = ['shipping_on_time', 'chat_resp_time', 'authentic_rating', 'day_return', 'waranty_period']

    def validate(self, data):
        # Validate social media URLs
        for field in ['facebook_url', 'instagram_url', 'twitter_url', 'linkedin_url']:
            url = data.get(field, '')
            if url:
                try:
                    URLValidator()(url)
                    # Basic URL format check for specific platforms
                    if field == 'facebook_url' and 'facebook.com' not in url.lower():
                        raise serializers.ValidationError({field: 'Invalid Facebook URL.'})
                    if field == 'instagram_url' and 'instagram.com' not in url.lower():
                        raise serializers.ValidationError({field: 'Invalid Instagram URL.'})
                    if field == 'twitter_url' and 'twitter.com' not in url.lower() and 'x.com' not in url.lower():
                        raise serializers.ValidationError({field: 'Invalid Twitter/X URL.'})
                    if field == 'linkedin_url' and 'linkedin.com' not in url.lower():
                        raise serializers.ValidationError({field: 'Invalid LinkedIn URL.'})
                except ValidationError:
                    raise serializers.ValidationError({field: f'Invalid URL format for {field.replace("_url", "").capitalize()}.'})

        # Validate latitude and longitude
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        if latitude is not None and not (-90 <= latitude <= 90):
            raise serializers.ValidationError({'latitude': 'Latitude must be between -90 and 90.'})
        if longitude is not None and not (-180 <= longitude <= 180):
            raise serializers.ValidationError({'longitude': 'Longitude must be between -180 and 180.'})

        # Validate vendor_name uniqueness
        vendor = self.context['request'].user.vendor_user
        new_vendor_name = data.get('vendor', {}).get('name', vendor.name)
        if new_vendor_name != vendor.name and Vendor.objects.filter(name=new_vendor_name).exists():
            raise serializers.ValidationError({'vendor_name': 'This store name is already taken.'})

        return data

    def update(self, instance, validated_data):
        # Update vendor name if changed
        vendor_data = validated_data.pop('vendor', {})
        if vendor_data.get('name'):
            instance.vendor.name = vendor_data['name']
            instance.vendor.slug = None  # Reset slug to regenerate
            instance.vendor.save()

        # Update About fields
        return super().update(instance, validated_data)
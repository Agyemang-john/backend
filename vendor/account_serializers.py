import re
from rest_framework import serializers
from django_countries.serializers import CountryFieldMixin
from .models import Vendor


class VendorAccountSerializer(CountryFieldMixin, serializers.ModelSerializer):
    """Read / partial-update the core Vendor row (account fields + documents)."""

    class Meta:
        model = Vendor
        fields = [
            'id', 'name', 'slug', 'email', 'contact', 'country',
            'vendor_type', 'business_type', 'is_manufacturer',
            'license', 'student_id', 'proof_of_address', 'government_issued_id',
            'status', 'is_approved', 'is_suspended', 'is_subscribed',
            'subscription_start_date', 'subscription_end_date',
            'created_at', 'modified_at', 'views',
        ]
        read_only_fields = [
            'id', 'slug', 'vendor_type',
            'status', 'is_approved', 'is_suspended',
            'is_subscribed', 'subscription_start_date', 'subscription_end_date',
            'created_at', 'modified_at', 'views',
        ]
        extra_kwargs = {
            'name': {'min_length': 2},
            'license': {'required': False, 'allow_null': True},
            'student_id': {'required': False, 'allow_null': True},
            'proof_of_address': {'required': False, 'allow_null': True},
            'government_issued_id': {'required': False, 'allow_null': True},
            'email': {'required': False, 'allow_null': True, 'allow_blank': True},
        }

    def validate_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Store name must be at least 2 characters.")
        if Vendor.objects.filter(name=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("This store name is already taken.")
        return value

    def validate_contact(self, value):
        if not re.match(r'^\+?\d{9,15}$', value):
            raise serializers.ValidationError(
                "Enter a valid phone number with country code (e.g., +233XXXXXXXXX)."
            )
        return value

    def update(self, instance, validated_data):
        if 'name' in validated_data and validated_data['name'] != instance.name:
            instance.slug = None  # Triggers slug re-generation in Vendor.save()
        return super().update(instance, validated_data)

# subscriptions/momo_serializers.py

from rest_framework import serializers
from .momo_models import MomoAccount, BillingProfile


class MomoAccountSerializer(serializers.ModelSerializer):
    provider_display = serializers.CharField(source='get_provider_display', read_only=True)
    masked_phone     = serializers.CharField(read_only=True)
    display_name     = serializers.CharField(read_only=True)

    class Meta:
        model  = MomoAccount
        fields = [
            'id', 'provider', 'provider_display',
            'phone', 'nickname', 'masked_phone', 'display_name',
            'is_default', 'last_reference', 'created_at',
        ]
        read_only_fields = ['id', 'last_reference', 'created_at']

    def validate_phone(self, value):
        """Normalise phone: strip spaces, ensure +233 prefix for GH numbers."""
        phone = value.replace(' ', '').replace('-', '')
        if phone.startswith('0') and len(phone) == 10:
            phone = '+233' + phone[1:]
        if not phone.startswith('+'):
            phone = '+' + phone
        return phone


class BillingProfileSerializer(serializers.ModelSerializer):
    full_name   = serializers.CharField(read_only=True)
    is_complete = serializers.BooleanField(read_only=True)

    class Meta:
        model  = BillingProfile
        fields = [
            'id',
            'first_name', 'last_name', 'full_name',
            'email', 'phone', 'business_name',
            'address_line1', 'address_line2',
            'city', 'region', 'postal_code', 'country',
            'is_complete',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InitiateMomoSerializer(serializers.Serializer):
    """Validates body for POST /momo/initiate/"""
    plan_id  = serializers.IntegerField()
    billing  = serializers.ChoiceField(choices=['monthly', 'quarterly', 'yearly'])
    phone    = serializers.CharField(max_length=20)
    provider = serializers.ChoiceField(choices=['mtn', 'vodafone', 'airteltigo'])
    save     = serializers.BooleanField(default=False, help_text="Save this number for future use")

    def validate_phone(self, value):
        phone = value.replace(' ', '').replace('-', '')
        if phone.startswith('0') and len(phone) == 10:
            phone = '+233' + phone[1:]
        if not phone.startswith('+'):
            phone = '+' + phone
        return phone


class ManualMomoSerializer(serializers.Serializer):
    """Validates body for POST /momo/pay-now/ (manual renewal)"""
    momo_id  = serializers.IntegerField(required=False, help_text="Use a saved MoMo account")
    phone    = serializers.CharField(max_length=20, required=False, help_text="Or enter a phone number directly")
    provider = serializers.ChoiceField(choices=['mtn', 'vodafone', 'airteltigo'], required=False)

    def validate(self, data):
        if not data.get('momo_id') and not (data.get('phone') and data.get('provider')):
            raise serializers.ValidationError(
                "Provide either momo_id (saved account) or both phone and provider."
            )
        return data
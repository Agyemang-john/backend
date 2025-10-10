from rest_framework import serializers
from .models import Vendor, About, VendorPaymentMethod
from django.core.validators import MinLengthValidator, RegexValidator
from django_countries.serializers import CountryFieldMixin
import re

class AboutSerializer(serializers.ModelSerializer):
    class Meta:
        model = About
        fields = [
            'profile_image', 'cover_image', 'address', 'about', 'latitude', 'longitude',
            'shipping_on_time', 'chat_resp_time', 'authentic_rating', 'day_return',
            'facebook_url', 'instagram_url', 'twitter_url', 'linkedin_url'
        ]
        extra_kwargs = {
            'shipping_on_time': {'required': False},
            'chat_resp_time': {'required': False},
            'authentic_rating': {'required': False},
            'day_return': {'required': False},
            'facebook_url': {'required': False},
            'instagram_url': {'required': False},
            'twitter_url': {'required': False},
            'linkedin_url': {'required': False},
        }

class VendorPaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorPaymentMethod
        fields = [
            'payment_method', 'momo_number', 'momo_provider', 'bank_name',
            'bank_account_name', 'bank_account_number',
            'country', 'currency'
        ]
        extra_kwargs = {
            'momo_number': {'required': False},
            'momo_provider': {'required': False},
            'bank_name': {'required': False},
            'bank_account_name': {'required': False},
            'bank_account_number': {'required': False},
            'country': {'required': True},
            'currency': {'required': True},
        }

    def validate(self, data):
        payment_method = data.get('payment_method')
        errors = {}
        if payment_method == 'momo':
            if not data.get('momo_number'):
                errors['momo_number'] = "Mobile money number is required."
            if not data.get('momo_provider'):
                errors['momo_provider'] = "Mobile money provider is required."
        elif payment_method == 'bank':
            if not data.get('bank_name'):
                errors['bank_name'] = "Bank name is required."
            if not data.get('bank_account_name'):
                errors['bank_account_name'] = "Bank account name is required."
            if not data.get('bank_account_number'):
                errors['bank_account_number'] = "Bank account number is required."
        elif payment_method == 'paypal':
            if not data.get('momo_number'):
                errors['momo_number'] = "PayPal email or phone is required."
            elif not re.match(r'^\S+@\S+\.\S+$|^\+?\d{10,15}$', data.get('momo_number')):
                errors['momo_number'] = "PayPal email or phone must be a valid email or 10-15 digit phone number."
        if not data.get('country'):
            errors['country'] = "Country is required."
        if not data.get('currency'):
            errors['currency'] = "Currency is required."
        if errors:
            raise serializers.ValidationError(errors)
        return data

class VendorSignupSerializer(CountryFieldMixin, serializers.ModelSerializer):
    about = AboutSerializer()
    payment_method = VendorPaymentMethodSerializer()  # Nested serializer for payment method

    class Meta:
        model = Vendor
        fields = [
            'name', 'email', 'country', 'contact', 'vendor_type', 'business_type',
            'license', 'student_id', 'proof_of_address', 'government_issued_id',
            'about', 'payment_method'
        ]
        extra_kwargs = {
            'name': {'validators': [MinLengthValidator(2)]},
            'contact': {'validators': [RegexValidator(r'^\+?1?\d{9,15}$')]},
            'license': {'required': False},
            'student_id': {'required': False},
            'proof_of_address': {'required': False},
            'government_issued_id': {'required': False},
        }

    def validate(self, data):
        vendor_type = data.get('vendor_type')
        if vendor_type == 'student' and not data.get('student_id'):
            raise serializers.ValidationError("Students must upload a valid student ID.")
        if vendor_type == 'non_student' and not data.get('government_issued_id'):
            raise serializers.ValidationError("Non-students must upload a valid government-issued ID.")
        return data

    def create(self, validated_data):
        about_data = validated_data.pop('about', None)
        payment_method_data = validated_data.pop('payment_method')
        user = self.context['request'].user

        if Vendor.objects.filter(user=user).exists():
            raise serializers.ValidationError("User already has a vendor profile.")
        
        vendor = Vendor.objects.create(user=user, **validated_data)

        if about_data:
            About.objects.update_or_create(
                vendor=vendor,
                defaults=about_data
            )

        VendorPaymentMethod.objects.create(
            vendor=vendor,
            last_updated_by=user,
            **payment_method_data
        )
        return vendor
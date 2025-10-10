from rest_framework import serializers
from .models import VendorPaymentMethod
import re
from order.models import Order
from payments.models import Payout

class VendorPaymentMethodSerializer(serializers.ModelSerializer):
    payment_method_display = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    vendor = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = VendorPaymentMethod
        fields = [
            'id',
            'vendor',
            'payment_method',
            'payment_method_display',
            'momo_number',
            'momo_provider',
            'bank_name',
            'bank_account_name',
            'bank_account_number',
            'country',
            'currency',
            'status',
            'status_display',
            'created_at',
            'updated_at',
            'last_updated_by',
        ]
        read_only_fields = [
            'id',
            'vendor',
            'status',
            'status_display',
            'created_at',
            'updated_at',
            'last_updated_by',
        ]

    def get_payment_method_display(self, obj):
        return obj.get_payment_method_display()

    def get_status_display(self, obj):
        return obj.get_status_display()

    def validate(self, data):
        payment_method = data.get('payment_method')
        errors = {}

        # Validate raw input from initial_data
        momo_number = self.initial_data.get('momo_number')
        bank_account_number = self.initial_data.get('bank_account_number')

        if payment_method == 'momo':
            if not momo_number:
                errors['momo_number'] = "Mobile Money number is required."
            elif not re.match(r'^\+?\d{10,15}$', momo_number):
                errors['momo_number'] = "Mobile Money number must be a valid phone number (10-15 digits, optional + prefix)."
            if not data.get('momo_provider'):
                errors['momo_provider'] = "Mobile Money provider is required."
            data['bank_name'] = None
            data['bank_account_name'] = None
            data['bank_account_number'] = None
        elif payment_method == 'bank':
            if not data.get('bank_name'):
                errors['bank_name'] = "Bank name is required."
            if not data.get('bank_account_name'):
                errors['bank_account_name'] = "Bank account name is required."
            if not bank_account_number:
                errors['bank_account_number'] = "Bank account number is required."
            elif not re.match(r'^\d{8,50}$', bank_account_number):
                errors['bank_account_number'] = "Bank account number must be 8-50 digits."
            data['momo_number'] = None
            data['momo_provider'] = None
        elif payment_method == 'paypal':
            if not momo_number:
                errors['momo_number'] = "PayPal email or phone is required."
            elif not re.match(r'^\S+@\S+\.\S+$|^\+?\d{10,15}$', momo_number):
                errors['momo_number'] = "PayPal email or phone must be a valid email or 10-15 digit phone number."
            data['momo_provider'] = None
            data['bank_name'] = None
            data['bank_account_name'] = None
            data['bank_account_number'] = None
        else:
            errors['payment_method'] = "Invalid payment method."

        if not data.get('country'):
            errors['country'] = "Country is required."
        if not data.get('currency'):
            errors['currency'] = "Currency is required."

        if errors:
            raise serializers.ValidationError(errors)

        return data


class OrderSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(read_only=True)

    class Meta:
        model = Order
        fields = ['id', 'order_number']

class PayoutSerializer(serializers.ModelSerializer):
    vendor = serializers.StringRelatedField(read_only=True)
    order = OrderSerializer(many=True, read_only=True)
    status_display = serializers.SerializerMethodField()

    class Meta:
        model = Payout
        fields = [
            'id',
            'vendor',
            'amount',
            'product_total',
            'delivery_fee',
            'status',
            'status_display',
            'transaction_id',
            'error_message',
            'order',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'vendor',
            'status_display',
            'order',
            'created_at',
            'updated_at',
        ]

    def get_status_display(self, obj):
        return obj.get_status_display()

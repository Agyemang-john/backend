# subscriptions/billing_serializers.py
# Serializers for the vendor billing dashboard.
# Covers: plan display, subscription status, payment history, saved cards.

from rest_framework import serializers
from .models import (
    SubscriptionPlan, VendorSubscription,
    SubscriptionUsage, PaymentTransaction,
    PaystackAuthorization, PaystackCustomer,
)


class BillingPlanSerializer(serializers.ModelSerializer):
    price_formatted   = serializers.SerializerMethodField()
    commission_display = serializers.SerializerMethodField()
    billing_cycle_display = serializers.CharField(source='get_billing_cycle_display')
    tier_display      = serializers.CharField(source='get_tier_display')

    class Meta:
        model  = SubscriptionPlan
        fields = [
            'id', 'name', 'tier', 'tier_display',
            'billing_cycle', 'billing_cycle_display',
            'price', 'price_formatted',
            'max_products', 'max_images_per_product', 'max_images_per_variant', 'max_categories',
            'can_feature_products', 'can_use_analytics', 'can_offer_discounts',
            'can_access_bulk_upload', 'can_use_storefront_customization',
            'priority_support', 'is_featured_vendor',
            'can_bulk_edit', 'can_use_stock_forecast', 'listing_assistant_monthly_limit',
            'can_use_profit_report', 'can_use_price_insights', 'can_use_auto_repricer',
            'can_use_demand_insights', 'can_use_promotions', 'broadcasts_monthly_limit', 'broadcast_sms_monthly_limit', 'can_print_labels',
            'can_use_chat_tools', 'can_use_store_link', 'can_use_api',
            'commission_rate', 'commission_display',
            'payout_delay_days', 'is_recommended', 'description',
        ]

    def get_price_formatted(self, obj):
        return f"GHS {obj.price:,.2f}"

    def get_commission_display(self, obj):
        return f"{obj.commission_rate}%"


class BillingSubscriptionSerializer(serializers.ModelSerializer):
    plan            = BillingPlanSerializer(read_only=True)
    days_remaining  = serializers.SerializerMethodField()
    is_active       = serializers.SerializerMethodField()
    status_display  = serializers.CharField(source='get_status_display')
    next_billing_date = serializers.SerializerMethodField()
    cancel_at_period_end = serializers.BooleanField(read_only=True)
    scheduled_plan  = BillingPlanSerializer(read_only=True)
    renewal_amount  = serializers.SerializerMethodField()

    class Meta:
        model  = VendorSubscription
        fields = [
            'id', 'plan', 'status', 'status_display',
            'start_date', 'end_date', 'trial_end_date',
            'auto_renew', 'cancelled_at', 'cancellation_reason',
            'payment_reference', 'days_remaining', 'is_active',
            'next_billing_date', 'cancel_at_period_end',
            'scheduled_plan', 'renewal_amount',
            # past_due: paid features stay on until grace_until; the seller
            # sees why the last renewal failed and can "Pay now".
            'grace_until', 'access_until', 'last_renewal_error',
        ]

    access_until = serializers.SerializerMethodField()

    def get_access_until(self, obj):
        return obj.access_until()

    def get_renewal_amount(self, obj):
        return str((obj.scheduled_plan or obj.plan).price)

    def get_days_remaining(self, obj):
        return obj.days_remaining()

    def get_is_active(self, obj):
        return obj.is_active()

    def get_next_billing_date(self, obj):
        """Next billing date = end_date when auto_renew is on."""
        if obj.auto_renew and obj.status in ('active', 'past_due'):
            return obj.end_date
        return None


class BillingUsageSerializer(serializers.ModelSerializer):
    max_products    = serializers.SerializerMethodField()
    usage_pct       = serializers.SerializerMethodField()
    can_add_product = serializers.SerializerMethodField()

    class Meta:
        model  = SubscriptionUsage
        fields = [
            'active_products_count', 'max_products', 'usage_pct',
            'can_add_product', 'period_start', 'period_end',
        ]

    def get_max_products(self, obj):
        from .entitlements import product_limit
        return product_limit(obj.vendor) or 0

    def get_usage_pct(self, obj):
        from .entitlements import product_limit
        limit = product_limit(obj.vendor)
        if limit:
            return round(obj.active_products_count / limit * 100, 1)
        return 0

    def get_can_add_product(self, obj):
        return obj.can_add_product()


class PaymentTransactionSerializer(serializers.ModelSerializer):
    plan_name      = serializers.SerializerMethodField()
    type_display   = serializers.CharField(source='get_transaction_type_display')
    status_display = serializers.CharField(source='get_status_display')
    amount_formatted = serializers.SerializerMethodField()
    card_last4     = serializers.SerializerMethodField()

    class Meta:
        model  = PaymentTransaction
        fields = [
            'id', 'transaction_type', 'type_display',
            'amount', 'amount_formatted', 'currency',
            'status', 'status_display',
            'paystack_reference', 'paystack_transaction_id',
            'plan_name', 'card_last4',
            'failure_reason', 'paid_at', 'created_at',
        ]

    def get_plan_name(self, obj):
        if obj.subscription and obj.subscription.plan:
            return obj.subscription.plan.name
        return None

    def get_amount_formatted(self, obj):
        return f"GHS {obj.amount:,.2f}"

    def get_card_last4(self, obj):
        if obj.authorization:
            return f"{obj.authorization.card_type.title()} •••• {obj.authorization.last4}"
        return None


class SavedCardSerializer(serializers.ModelSerializer):
    display_name   = serializers.SerializerMethodField()
    expiry_display = serializers.SerializerMethodField()
    is_expired     = serializers.SerializerMethodField()

    class Meta:
        model  = PaystackAuthorization
        fields = [
            'id', 'card_type', 'last4', 'exp_month', 'exp_year',
            'bank', 'is_default', 'is_reusable',
            'display_name', 'expiry_display', 'is_expired', 'created_at',
        ]

    def get_display_name(self, obj):
        return f"{obj.card_type.title()} •••• {obj.last4}"

    def get_expiry_display(self, obj):
        return f"{obj.exp_month}/{obj.exp_year}"

    def get_is_expired(self, obj):
        from django.utils import timezone
        from datetime import datetime
        try:
            exp = datetime(int(obj.exp_year), int(obj.exp_month), 1)
            return exp < timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        except Exception:
            return False


class BillingOverviewSerializer(serializers.Serializer):
    """
    Combined serializer for the billing overview tab.
    Returns subscription + usage + recent transactions in one request.
    """
    subscription       = BillingSubscriptionSerializer(allow_null=True)
    usage              = BillingUsageSerializer(allow_null=True)
    recent_transactions = PaymentTransactionSerializer(many=True)
    saved_cards        = SavedCardSerializer(many=True)
    available_plans    = BillingPlanSerializer(many=True)
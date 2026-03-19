# subscriptions/serializers.py

from rest_framework import serializers
from .models import (
    SubscriptionPlan,
    VendorSubscription,
    SubscriptionUsage,
    PaystackCustomer,
    PaystackAuthorization,
    PaymentTransaction,
)


# ─────────────────────────────────────────────────────────────────────────────
# Subscription Plan
# ─────────────────────────────────────────────────────────────────────────────

class SubscriptionPlanSerializer(serializers.ModelSerializer):
    """
    Full read-only serializer for plan listing page.
    The frontend uses this to populate the plan cards dynamically
    instead of having plans hardcoded in the TSX file.
    """
    billing_cycle_display = serializers.CharField(
        source='get_billing_cycle_display', read_only=True
    )
    tier_display = serializers.CharField(
        source='get_tier_display', read_only=True
    )
    price_formatted = serializers.SerializerMethodField()
    commission_display = serializers.SerializerMethodField()

    class Meta:
        model = SubscriptionPlan
        fields = [
            'id',
            'name',
            'tier',
            'tier_display',
            'billing_cycle',
            'billing_cycle_display',
            'price',
            'price_formatted',
            # Limits
            'max_products',
            'max_images_per_product',
            'max_categories',
            # Feature flags
            'can_feature_products',
            'can_use_analytics',
            'can_offer_discounts',
            'can_access_bulk_upload',
            'can_use_storefront_customization',
            'priority_support',
            'is_featured_vendor',
            # Financials
            'commission_rate',
            'commission_display',
            'payout_delay_days',
            # Metadata
            'is_active',
            'is_recommended',
            'description',
        ]

    def get_price_formatted(self, obj):
        return f"GHS {obj.price:,.2f}"

    def get_commission_display(self, obj):
        return f"{obj.commission_rate}%"


class SubscriptionPlanGroupedSerializer(serializers.Serializer):
    """
    Groups plans by tier so the frontend can display them in order.
    Returns: { free: plan, basic: plan, pro: plan, enterprise: plan }
    """
    free = SubscriptionPlanSerializer(read_only=True)
    basic = SubscriptionPlanSerializer(read_only=True)
    pro = SubscriptionPlanSerializer(read_only=True)
    enterprise = SubscriptionPlanSerializer(read_only=True)


# ─────────────────────────────────────────────────────────────────────────────
# Paystack Models
# ─────────────────────────────────────────────────────────────────────────────

class PaystackAuthorizationSerializer(serializers.ModelSerializer):
    """
    Safe representation of a saved card — never exposes the authorization_code.
    Used in the vendor dashboard to show "Visa **** 4081".
    """
    card_display = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = PaystackAuthorization
        fields = [
            'id',
            'card_type',
            'last4',
            'exp_month',
            'exp_year',
            'bank',
            'is_default',
            'is_reusable',
            'card_display',
            'is_expired',
            'created_at',
        ]
        # authorization_code is intentionally excluded — never expose this

    def get_card_display(self, obj):
        return f"{obj.card_type.title()} **** {obj.last4}"

    def get_is_expired(self, obj):
        from django.utils import timezone
        import datetime
        try:
            exp = datetime.date(int(obj.exp_year), int(obj.exp_month), 1)
            return exp < timezone.now().date().replace(day=1)
        except (ValueError, TypeError):
            return False


class PaystackCustomerSerializer(serializers.ModelSerializer):
    saved_cards = PaystackAuthorizationSerializer(
        source='paystackauthorization_set', many=True, read_only=True
    )

    class Meta:
        model = PaystackCustomer
        fields = ['id', 'customer_code', 'email', 'saved_cards', 'created_at']


# ─────────────────────────────────────────────────────────────────────────────
# Vendor Subscription
# ─────────────────────────────────────────────────────────────────────────────

class VendorSubscriptionSerializer(serializers.ModelSerializer):
    """
    Full subscription record — used in dashboard, renewal checks, etc.
    """
    plan = SubscriptionPlanSerializer(read_only=True)
    plan_id = serializers.PrimaryKeyRelatedField(
        queryset=SubscriptionPlan.objects.filter(is_active=True),
        source='plan',
        write_only=True
    )
    status_display = serializers.CharField(
        source='get_status_display', read_only=True
    )
    days_remaining = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()
    is_on_trial = serializers.SerializerMethodField()
    renewal_amount = serializers.SerializerMethodField()

    class Meta:
        model = VendorSubscription
        fields = [
            'id',
            'plan',
            'plan_id',
            'status',
            'status_display',
            'start_date',
            'end_date',
            'trial_end_date',
            'auto_renew',
            'cancelled_at',
            'cancellation_reason',
            'payment_reference',
            'days_remaining',
            'is_active',
            'is_on_trial',
            'renewal_amount',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'status', 'start_date', 'end_date', 'cancelled_at',
            'payment_reference', 'created_at', 'updated_at',
        ]

    def get_days_remaining(self, obj):
        return obj.days_remaining()

    def get_is_active(self, obj):
        return obj.is_active()

    def get_is_on_trial(self, obj):
        return obj.is_on_trial()

    def get_renewal_amount(self, obj):
        return float(obj.plan.price)


class ActiveSubscriptionSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for header/nav usage — just the essentials.
    Includes plan_billing_cycle so the frontend can match the exact plan
    card (tier + billing cycle) rather than just the tier slug.
    """
    plan_name         = serializers.CharField(source='plan.name', read_only=True)
    plan_tier         = serializers.CharField(source='plan.tier', read_only=True)
    plan_billing_cycle = serializers.CharField(source='plan.billing_cycle', read_only=True)
    days_remaining    = serializers.SerializerMethodField()

    class Meta:
        model = VendorSubscription
        fields = [
            'id', 'plan_name', 'plan_tier', 'plan_billing_cycle',
            'status', 'end_date', 'auto_renew', 'days_remaining',
        ]

    def get_days_remaining(self, obj):
        return obj.days_remaining()


# ─────────────────────────────────────────────────────────────────────────────
# Subscription Usage
# ─────────────────────────────────────────────────────────────────────────────

class SubscriptionUsageSerializer(serializers.ModelSerializer):
    can_add_product = serializers.SerializerMethodField()
    max_products = serializers.SerializerMethodField()
    usage_percentage = serializers.SerializerMethodField()

    class Meta:
        model = SubscriptionUsage
        fields = [
            'active_products_count',
            'can_add_product',
            'max_products',
            'usage_percentage',
            'period_start',
            'period_end',
            'updated_at',
        ]

    def get_can_add_product(self, obj):
        return obj.can_add_product()

    def get_max_products(self, obj):
        if obj.subscription and obj.subscription.plan:
            return obj.subscription.plan.max_products
        return 0

    def get_usage_percentage(self, obj):
        max_p = self.get_max_products(obj)
        if max_p == 0:
            return 100
        return round((obj.active_products_count / max_p) * 100, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Payment Transaction
# ─────────────────────────────────────────────────────────────────────────────

class PaymentTransactionSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(
        source='get_status_display', read_only=True
    )
    type_display = serializers.CharField(
        source='get_transaction_type_display', read_only=True
    )
    plan_name = serializers.CharField(
        source='subscription.plan.name', read_only=True
    )
    amount_formatted = serializers.SerializerMethodField()

    class Meta:
        model = PaymentTransaction
        fields = [
            'id',
            'transaction_type',
            'type_display',
            'amount',
            'amount_formatted',
            'currency',
            'status',
            'status_display',
            'paystack_reference',
            'plan_name',
            'failure_reason',
            'paid_at',
            'created_at',
        ]
        # paystack_transaction_id excluded — internal only

    def get_amount_formatted(self, obj):
        return f"GHS {obj.amount:,.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Serializers — used for API input validation
# ─────────────────────────────────────────────────────────────────────────────

class InitiateSubscriptionSerializer(serializers.Serializer):
    """
    Validates the POST body when vendor clicks 'Subscribe now'.
    plan_id: the SubscriptionPlan pk
    billing: monthly | yearly  (used to pick correct price tier)
    """
    BILLING_CHOICES = [('monthly', 'Monthly'), ('yearly', 'Yearly')]

    plan_id = serializers.IntegerField()
    billing = serializers.ChoiceField(choices=BILLING_CHOICES, default='monthly')

    def validate_plan_id(self, value):
        try:
            plan = SubscriptionPlan.objects.get(pk=value, is_active=True)
        except SubscriptionPlan.DoesNotExist:
            raise serializers.ValidationError("Plan not found or inactive.")
        return value


class CancelSubscriptionSerializer(serializers.Serializer):
    """Validates cancellation request — reason is optional."""
    reason = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
        default=""
    )


class UpdateAutoRenewSerializer(serializers.Serializer):
    """Toggle auto-renewal on/off."""
    auto_renew = serializers.BooleanField()


class PaystackWebhookSerializer(serializers.Serializer):
    """
    Validates incoming Paystack webhook payload shape.
    Paystack sends { event: "charge.success", data: { ... } }
    """
    event = serializers.CharField()
    data = serializers.DictField()

    def validate_event(self, value):
        allowed = [
            'charge.success',
            'charge.failed',
            'subscription.create',
            'subscription.not_renew',
            'invoice.payment_failed',
            'refund.processed',
        ]
        if value not in allowed:
            raise serializers.ValidationError(f"Unhandled event: {value}")
        return value
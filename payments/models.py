from django.db import models
from django.utils import timezone
import secrets
from vendor.models import *
from order.models import Order
from django.conf import settings
# from paystack import Paystack

    
class Payment(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name='payments', blank=True, null=True)
    amount = models.PositiveIntegerField()
    ref = models.CharField(max_length=200)
    email = models.EmailField()
    verified = models.BooleanField(default=False)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-date_created',)
    
    def __str__(self):
        return f"Payments: {self.amount}"
    
    def save(self, *args, **kwargs):
        while not self.ref:
            ref = secrets.token_urlsafe(50)
            object_with_similar_ref = Payment.objects.filter(ref=ref)
            if not object_with_similar_ref:
                self.ref = ref
        super().save(*args, **kwargs)


class Payout(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    product_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=[('success', 'Success'), ('failed', 'Failed')])
    transaction_id = models.CharField(max_length=100, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    order = models.ManyToManyField(Order, related_name='payouts')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


from django.utils import timezone
from django.core.validators import MinValueValidator
from datetime import timedelta


class SubscriptionPlan(models.Model):
    """
    Defines available subscription tiers (e.g., Free, Basic, Pro, Enterprise).
    Admins create and manage these plans.
    """
    PLAN_TIER_CHOICES = [
        ('free', 'Free'),
        ('basic', 'Basic'),
        ('pro', 'Pro'),
        ('enterprise', 'Enterprise'),
    ]

    BILLING_CYCLE_CHOICES = [
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('yearly', 'Yearly'),
    ]

    name = models.CharField(max_length=100, unique=True)
    tier = models.CharField(max_length=20, choices=PLAN_TIER_CHOICES, default='free')
    billing_cycle = models.CharField(max_length=20, choices=BILLING_CYCLE_CHOICES, default='monthly')
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    # Product & Listing Limits
    max_products = models.PositiveIntegerField(
        default=10,
        help_text="Maximum number of active product listings allowed."
    )
    max_images_per_product = models.PositiveIntegerField(
        default=3,
        help_text="Maximum number of images per product listing."
    )
    max_categories = models.PositiveIntegerField(
        default=2,
        help_text="Maximum number of product categories a vendor can list in."
    )

    # Feature Flags
    can_feature_products = models.BooleanField(
        default=False,
        help_text="Can vendor boost/feature individual products?"
    )
    can_use_analytics = models.BooleanField(
        default=False,
        help_text="Access to advanced sales and traffic analytics."
    )
    can_offer_discounts = models.BooleanField(
        default=False,
        help_text="Can vendor create discount codes and promotions?"
    )
    can_access_bulk_upload = models.BooleanField(
        default=False,
        help_text="Can vendor upload products in bulk via CSV/spreadsheet?"
    )
    can_use_storefront_customization = models.BooleanField(
        default=False,
        help_text="Access to custom storefront themes and branding."
    )
    priority_support = models.BooleanField(
        default=False,
        help_text="Vendor gets priority customer support."
    )
    is_featured_vendor = models.BooleanField(
        default=False,
        help_text="Vendor profile is featured/promoted on the platform."
    )

    # Commission & Financials
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=10.00,
        validators=[MinValueValidator(0)],
        help_text="Platform commission percentage on sales (e.g., 10.00 = 10%)."
    )
    payout_delay_days = models.PositiveIntegerField(
        default=7,
        help_text="Number of days before earnings are released to vendor."
    )

    # Plan Visibility
    is_active = models.BooleanField(default=True, help_text="Whether this plan is publicly available.")
    is_recommended = models.BooleanField(default=False, help_text="Highlight this plan as recommended.")
    description = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.get_billing_cycle_display()}) - GHS {self.price}"

    class Meta:
        verbose_name = "Subscription Plan"
        verbose_name_plural = "Subscription Plans"
        ordering = ['price']


class VendorSubscription(models.Model):
    """
    Tracks a vendor's current and historical subscriptions.
    One active subscription per vendor at a time.
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
        ('trial', 'Trial'),
        ('past_due', 'Past Due'),
    ]

    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name='subscriptions'
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,  # Prevent deleting plans with active subscribers
        related_name='vendor_subscriptions'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    start_date = models.DateTimeField(default=timezone.now)
    end_date = models.DateTimeField()
    trial_end_date = models.DateTimeField(
        null=True, blank=True,
        help_text="If on a trial, when does it end?"
    )

    # Auto-renewal
    auto_renew = models.BooleanField(default=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True, null=True)

    # Payment Reference (link to your payment model/gateway)
    payment_reference = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Payment gateway transaction ID (e.g., Paystack reference)."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_active(self):
        """Returns True if this subscription is currently active."""
        return self.status == 'active' and self.end_date >= timezone.now()

    def is_on_trial(self):
        return self.status == 'trial' and self.trial_end_date and self.trial_end_date >= timezone.now()

    def days_remaining(self):
        """Returns days left on the subscription."""
        if self.end_date:
            delta = self.end_date - timezone.now()
            return max(delta.days, 0)
        return 0

    def cancel(self, reason=None):
        """Cancel this subscription."""
        self.status = 'cancelled'
        self.auto_renew = False
        self.cancelled_at = timezone.now()
        if reason:
            self.cancellation_reason = reason
        self.save()

    def renew(self):
        """Renew the subscription by extending the end date."""
        billing_cycle_days = {
            'monthly': 30,
            'quarterly': 90,
            'yearly': 365,
        }
        days = billing_cycle_days.get(self.plan.billing_cycle, 30)
        self.end_date = timezone.now() + timedelta(days=days)
        self.status = 'active'
        self.save()

    def __str__(self):
        return f"{self.vendor.name} - {self.plan.name} ({self.status})"

    class Meta:
        verbose_name = "Vendor Subscription"
        verbose_name_plural = "Vendor Subscriptions"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'status']),
        ]


class SubscriptionUsage(models.Model):
    """
    Tracks real-time usage against plan limits for the current billing period.
    Reset at the start of each new billing cycle.
    """
    vendor = models.OneToOneField(
        Vendor,
        on_delete=models.CASCADE,
        related_name='subscription_usage'
    )
    subscription = models.ForeignKey(
        VendorSubscription,
        on_delete=models.SET_NULL,
        null=True,
        related_name='usage_records'
    )

    active_products_count = models.PositiveIntegerField(default=0)
    period_start = models.DateTimeField(default=timezone.now)
    period_end = models.DateTimeField(null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    def can_add_product(self):
        from payments.models import SubscriptionPlan
        if self.subscription:
            plan = self.subscription.plan
        else:
            # Fall back to free plan limits
            plan = SubscriptionPlan.objects.filter(tier="free").order_by("price").first()
        if not plan:
            return True  # no plan configured at all — allow
        return self.active_products_count < plan.max_products

    def reset_for_new_cycle(self):
        """Call this when a new billing period starts."""
        self.active_products_count = 0
        self.period_start = timezone.now()
        self.save()

    def __str__(self):
        return f"Usage: {self.vendor.name}"

    class Meta:
        verbose_name = "Subscription Usage"
        verbose_name_plural = "Subscription Usages"



# Renewals, cancellations, and upgrades would be handled in the service layer, not the model.
class PaystackCustomer(models.Model):
    """Stores Paystack customer data per vendor."""
    vendor = models.OneToOneField(Vendor, on_delete=models.CASCADE, related_name='paystack_customer')
    customer_code = models.CharField(max_length=100, unique=True)  # e.g., CUS_xxxxxxxx
    email = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.vendor.name} - {self.customer_code}"


class PaystackAuthorization(models.Model):
    """
    Stores reusable card authorizations from Paystack.
    This is what enables auto-billing without the vendor re-entering card details.
    """
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='authorizations')
    paystack_customer = models.ForeignKey(PaystackCustomer, on_delete=models.CASCADE)
    
    authorization_code = models.CharField(max_length=100)  # The magic token — e.g., AUTH_xxxxxxxx
    card_type = models.CharField(max_length=50)            # "visa", "mastercard"
    last4 = models.CharField(max_length=4)                 # "4081" — show this to vendor
    exp_month = models.CharField(max_length=2)
    exp_year = models.CharField(max_length=4)
    bank = models.CharField(max_length=100, blank=True)
    
    is_default = models.BooleanField(default=False)        # Which card to charge
    is_reusable = models.BooleanField(default=True)        # Paystack confirms this
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.card_type} **** {self.last4} ({self.vendor.name})"


class PaymentTransaction(models.Model):
    """Full audit trail of every charge attempt."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]
    TYPE_CHOICES = [
        ('initial', 'Initial Subscription'),
        ('renewal', 'Auto Renewal'),
        ('upgrade', 'Plan Upgrade'),
        ('manual', 'Manual Payment'),
    ]

    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    subscription = models.ForeignKey(VendorSubscription, on_delete=models.SET_NULL, null=True)
    authorization = models.ForeignKey(PaystackAuthorization, on_delete=models.SET_NULL, null=True, blank=True)

    transaction_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=5, default='GHS')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    paystack_reference = models.CharField(max_length=200, unique=True)  # Your unique ref
    paystack_transaction_id = models.CharField(max_length=200, blank=True, null=True)  # Paystack's ID
    
    failure_reason = models.TextField(blank=True, null=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.vendor.name} - {self.amount} {self.currency} ({self.status})"

from .email_models import EmailTemplate, SubscriptionEmailConfig

EmailTemplate
SubscriptionEmailConfig

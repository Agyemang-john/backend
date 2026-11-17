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
    # processing: ledger entries are reserved and the transfer is in flight.
    # success: money sent; failed: entries released back to the balance.
    STATUS_CHOICES = [
        ('processing', 'Processing'),
        ('success', 'Paid'),
        ('failed', 'Failed'),
    ]
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    product_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
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
    max_images_per_variant = models.PositiveIntegerField(
        default=0,
        help_text="Extra gallery photos per variant, on top of its main photo "
                  "(0 = not included in this plan). Stored on Spaces, so it is a paid feature."
    )
    max_categories = models.PositiveIntegerField(
        default=2,
        help_text="Maximum number of product categories a vendor can list in."
    )
    max_team_members = models.PositiveIntegerField(
        default=1,
        help_text="People who can sign in to the store's Seller Centre, owner included "
                  "(1 = owner only). Pending invitations count towards the limit."
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
    can_export_reports = models.BooleanField(
        default=False,
        help_text="Vendor can download orders and account statements as CSV."
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

    # A downgrade waits for the end of the paid period; the next renewal
    # charges and switches to this plan.
    scheduled_plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
        help_text="Plan to switch to at the next renewal (a scheduled downgrade).",
    )

    # ── Renewal / dunning state (payments/subscription_lifecycle.py) ──────
    # A failed renewal moves the store to past_due at end_date; paid features
    # stay on until grace_until while renewal is retried, then it expires.
    grace_until = models.DateTimeField(null=True, blank=True)
    renewal_attempts = models.PositiveSmallIntegerField(
        default=0, help_text="Renewal charges tried for the current period.")
    next_renewal_attempt_at = models.DateTimeField(
        null=True, blank=True, help_text="Earliest time the next renewal charge may run.")
    last_renewal_error = models.CharField(max_length=255, blank=True, default='')
    # Last reminder sent for this period, so each reminder goes out once.
    last_reminder_key = models.CharField(max_length=64, blank=True, default='')

    # Payment Reference (link to your payment model/gateway)
    payment_reference = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Payment gateway transaction ID (e.g., Paystack reference)."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Statuses that may still grant a plan (subject to dates; see in_force()).
    LIVE_STATUSES = ('active', 'trial', 'past_due')

    def is_active(self):
        """Returns True if this subscription is currently active."""
        return self.status == 'active' and self.end_date >= timezone.now()

    def access_until(self):
        """When this subscription stops granting its plan."""
        if self.status == 'past_due':
            return self.grace_until or self.end_date
        if self.status == 'trial' and self.trial_end_date:
            return min(self.trial_end_date, self.end_date)
        return self.end_date

    def in_force(self, now=None):
        """True while the store should get this plan's features."""
        now = now or timezone.now()
        return self.status in self.LIVE_STATUSES and self.access_until() > now

    def is_on_trial(self):
        return self.status == 'trial' and self.trial_end_date and self.trial_end_date >= timezone.now()

    def days_remaining(self):
        """Returns days left on the subscription."""
        if self.end_date:
            delta = self.end_date - timezone.now()
            return max(delta.days, 0)
        return 0

    @property
    def cancel_at_period_end(self):
        return self.status == 'active' and self.cancelled_at is not None and not self.auto_renew

    def cancel(self, reason=None):
        """
        Stop renewing. The store keeps its plan until end_date, then
        expire_old_subscriptions moves it to Free. status stays 'active'
        so entitlements keep working for the days already paid for.
        """
        self.auto_renew = False
        self.cancelled_at = timezone.now()
        self.scheduled_plan = None
        if reason:
            self.cancellation_reason = reason
        self.save()

    def resume(self):
        """Undo a cancellation before the period ends."""
        self.auto_renew = True
        self.cancelled_at = None
        self.cancellation_reason = None
        self.save()

    def renew(self, plan=None):
        """
        Extend by one billing period of `plan` (default: the scheduled plan if
        a downgrade is waiting, else the current plan).

        The new period starts at end_date, not now: renewals run before
        end_date, and a store that pays during its grace period has already
        used those days. Only a lapsed (expired) subscription restarts today.
        """
        billing_cycle_days = {
            'monthly': 30,
            'quarterly': 90,
            'yearly': 365,
        }
        plan = plan or self.scheduled_plan or self.plan
        self.plan = plan
        self.scheduled_plan = None
        days = billing_cycle_days.get(plan.billing_cycle, 30)
        base = self.end_date if self.status in ('active', 'past_due') else timezone.now()
        self.end_date = base + timedelta(days=days)
        self.status = 'active'
        self.grace_until = None
        self.renewal_attempts = 0
        self.next_renewal_attempt_at = None
        self.last_renewal_error = ''
        self.save()

    def __str__(self):
        return f"{self.vendor.name} - {self.plan.name} ({self.status})"

    class Meta:
        verbose_name = "Vendor Subscription"
        verbose_name_plural = "Vendor Subscriptions"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'status']),
            # Renewal / expiry sweeps scan by status and date.
            models.Index(fields=['status', 'end_date'], name='sub_status_end_idx'),
        ]
        constraints = [
            # At most one subscription per store can grant a plan. Every
            # activation path cancels the old row first; this makes a race
            # or a bug fail loudly instead of leaving two live plans.
            models.UniqueConstraint(
                fields=['vendor'],
                condition=models.Q(status__in=('active', 'trial', 'past_due')),
                name='one_live_subscription_per_vendor',
            ),
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
        """Display helper; the enforced check is entitlements.reserve_product_slots."""
        from payments.entitlements import product_count, product_limit
        limit = product_limit(self.vendor)
        return limit is None or product_count(self.vendor) < limit

    def reset_for_new_cycle(self):
        """
        Start of a new billing period. The product count is NOT reset: it is
        the number of products the store has (resetting it let every plan
        change hand out a fresh listing allowance).
        """
        from payments.entitlements import product_count
        self.active_products_count = product_count(self.vendor)
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
        # Money arrived but nothing was applied (duplicate payment, quote no
        # longer valid…). Needs a person: refund or apply by hand.
        ('review', 'Needs review'),
    ]
    TYPE_CHOICES = [
        ('initial', 'Initial Subscription'),
        ('renewal', 'Auto Renewal'),
        ('upgrade', 'Plan Upgrade'),
        ('manual', 'Manual Payment'),
        ('ads', 'Sponsored products'),
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
    
    # What this payment buys, recorded when we start it. Activation reads
    # these, never the gateway metadata or a cache entry.
    plan = models.ForeignKey(
        SubscriptionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    billing = models.CharField(max_length=20, blank=True, default='')
    # Renewals: the end_date of the period being renewed. A second payment
    # for the same period is a duplicate and is never applied twice.
    period_end = models.DateTimeField(null=True, blank=True)

    failure_reason = models.TextField(blank=True, null=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['subscription', 'transaction_type', 'status'], name='txn_sub_type_status_idx'),
        ]

    def __str__(self):
        who = self.vendor.name if self.vendor_id else 'Deleted store'
        return f"{who} - {self.amount} {self.currency} ({self.status})"

from .email_models import EmailTemplate, SubscriptionEmailConfig

EmailTemplate
SubscriptionEmailConfig

from .ledger_models import LedgerEntry  # noqa: E402,F401  (seller money ledger)

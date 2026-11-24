from django.conf import settings
from django.db import models
from django.db.models import F
from django.utils import timezone


def month_start(today=None):
    today = today or timezone.localdate()
    return today.replace(day=1)


class ToolUsage(models.Model):
    """How much of a monthly allowance a store has used (one row per store, tool and month)."""
    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='tool_usage')
    tool = models.CharField(max_length=40)
    month = models.DateField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['vendor', 'tool', 'month'], name='uniq_tool_usage_month')]

    @classmethod
    def used(cls, vendor, tool):
        return cls.objects.filter(vendor=vendor, tool=tool, month=month_start()).values_list('count', flat=True).first() or 0

    @classmethod
    def take(cls, vendor, tool, limit):
        """
        Use one unit if the store has any left this month. Atomic (a single
        conditional UPDATE), so two requests can't both take the last unit.
        Returns True when a unit was taken.
        """
        month = month_start()
        cls.objects.get_or_create(vendor=vendor, tool=tool, month=month)
        return bool(cls.objects.filter(vendor=vendor, tool=tool, month=month, count__lt=limit)
                    .update(count=F('count') + 1))

    @classmethod
    def give_back(cls, vendor, tool):
        """Return a unit taken for work that then failed."""
        cls.objects.filter(vendor=vendor, tool=tool, month=month_start(), count__gt=0).update(count=F('count') - 1)

    @classmethod
    def take_many(cls, vendor, tool, n, limit):
        """Use n units at once if that many are left this month (atomic, like take)."""
        if n <= 0:
            return True
        month = month_start()
        cls.objects.get_or_create(vendor=vendor, tool=tool, month=month)
        return bool(cls.objects.filter(vendor=vendor, tool=tool, month=month, count__lte=limit - n)
                    .update(count=F('count') + n))

    @classmethod
    def give_back_many(cls, vendor, tool, n):
        if n > 0:
            cls.objects.filter(vendor=vendor, tool=tool, month=month_start(), count__gte=n).update(count=F('count') - n)


class RepricerRule(models.Model):
    """Keeps one listing's price competitive within the seller's limits (tools/repricer.py)."""
    BEAT = 'beat'          # undercut the cheapest other offer by `step`
    MATCH = 'match'        # match the cheapest other offer
    STRATEGY_CHOICES = [(BEAT, 'Beat the lowest price'), (MATCH, 'Match the lowest price')]

    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='repricer_rules')
    product = models.OneToOneField('product.Product', on_delete=models.CASCADE, related_name='repricer_rule')
    strategy = models.CharField(max_length=10, choices=STRATEGY_CHOICES, default=BEAT)
    step = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    min_price = models.DecimalField(max_digits=10, decimal_places=2)
    max_price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=160, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['is_active', 'last_run_at'])]


class PromotionRule(models.Model):
    """One automation of the promotions autopilot (tools/promotions.py); settings per kind there."""
    WIN_BACK = 'win_back'
    THANK_YOU = 'thank_you'
    WEEKLY_SALE = 'weekly_sale'
    KIND_CHOICES = [(WIN_BACK, 'Win back lapsed customers'), (THANK_YOU, 'Thank-you coupon'),
                    (WEEKLY_SALE, 'Weekly sale')]

    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='promotion_rules')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    is_active = models.BooleanField(default=True)
    settings = models.JSONField(default=dict)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['vendor', 'kind'], name='uniq_promotion_rule_kind')]


class PromotionSend(models.Model):
    """A personal coupon an automation sent to one shopper (for stats and to avoid re-sending)."""
    rule = models.ForeignKey(PromotionRule, on_delete=models.CASCADE, related_name='sends')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    coupon = models.ForeignKey('product.Coupon', on_delete=models.SET_NULL, null=True, related_name='+')
    emailed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['rule', 'user', '-created_at'])]


class PromotionSale(models.Model):
    """A flash sale the weekly-sale automation scheduled."""
    rule = models.ForeignKey(PromotionRule, on_delete=models.CASCADE, related_name='sales')
    flash_sale = models.OneToOneField('product.FlashSale', on_delete=models.CASCADE, related_name='+')
    starts_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=['rule', 'starts_at'])]


class Broadcast(models.Model):
    """A message a store sends its followers and/or past buyers (tools/broadcasts.py)."""
    FOLLOWERS = 'followers'
    BUYERS = 'buyers'
    EVERYONE = 'everyone'
    AUDIENCE_CHOICES = [(FOLLOWERS, 'Followers'), (BUYERS, 'Past buyers'), (EVERYONE, 'Followers and past buyers')]
    QUEUED, SENDING, SENT, FAILED = 'queued', 'sending', 'sent', 'failed'
    STATUS_CHOICES = [(QUEUED, 'Queued'), (SENDING, 'Sending'), (SENT, 'Sent'), (FAILED, 'Failed')]

    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='broadcasts')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    audience = models.CharField(max_length=12, choices=AUDIENCE_CHOICES)
    subject = models.CharField(max_length=80)
    message = models.TextField(max_length=1000)
    product_ids = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=QUEUED)
    recipients = models.PositiveIntegerField(default=0)
    emailed = models.PositiveIntegerField(default=0)
    with_sms = models.BooleanField(default=False)
    sms_reserved = models.PositiveIntegerField(default=0)      # SMS allowance taken when queued
    sms_sent = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['vendor', '-created_at'])]


class SavedReply(models.Model):
    """A reply the store's team inserts into shopper chat with one tap (tools/chat_tools.py)."""
    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='saved_replies')
    title = models.CharField(max_length=40)
    body = models.TextField(max_length=1000)
    use_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-use_count', 'title']


class AwaySetting(models.Model):
    """When and what the store answers automatically in shopper chat."""
    OFF = 'off'
    OUTSIDE_HOURS = 'outside_hours'
    ALWAYS = 'always'
    MODE_CHOICES = [(OFF, 'Off'), (OUTSIDE_HOURS, 'Outside opening hours'), (ALWAYS, 'Always (e.g. on holiday)')]

    vendor = models.OneToOneField('vendor.Vendor', on_delete=models.CASCADE, related_name='away_setting')
    mode = models.CharField(max_length=14, choices=MODE_CHOICES, default=OFF)
    message = models.TextField(max_length=500, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)


class StoreLink(models.Model):
    """negromart.com/s/<handle> → the store's page (tools/storefront.py)."""
    vendor = models.OneToOneField('vendor.Vendor', on_delete=models.CASCADE, related_name='store_link')
    handle = models.SlugField(max_length=30, unique=True)
    visits = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class StoreBranding(models.Model):
    """How the store's public page looks (shown while the plan includes the store tool)."""
    vendor = models.OneToOneField('vendor.Vendor', on_delete=models.CASCADE, related_name='branding')
    accent_color = models.CharField(max_length=7, blank=True, default='')
    tagline = models.CharField(max_length=80, blank=True, default='')
    announcement = models.CharField(max_length=140, blank=True, default='')
    featured_product_ids = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ApiKey(models.Model):
    """A key a store's own systems use to call the seller API (tools/seller_api.py). Only its hash is kept."""
    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='api_keys')
    name = models.CharField(max_length=60)
    prefix = models.CharField(max_length=12, unique=True)       # shown so sellers can tell keys apart
    key_hash = models.CharField(max_length=64)
    can_write = models.BooleanField(default=True)                # False: read-only key
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class Webhook(models.Model):
    """A URL of the store's that is told about its orders as they happen (tools/webhooks.py)."""
    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='webhooks')
    url = models.URLField(max_length=300)
    secret = models.CharField(max_length=64)
    events = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    disabled_reason = models.CharField(max_length=160, blank=True, default='')
    last_delivery_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class WebhookDelivery(models.Model):
    PENDING, SENT, FAILED = 'pending', 'sent', 'failed'
    STATUS_CHOICES = [(PENDING, 'Pending'), (SENT, 'Sent'), (FAILED, 'Failed')]

    webhook = models.ForeignKey(Webhook, on_delete=models.CASCADE, related_name='deliveries')
    event = models.CharField(max_length=40)
    payload = models.JSONField()
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    response_code = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['webhook', '-created_at']), models.Index(fields=['created_at'])]


class DemandTerm(models.Model):
    """
    What shoppers searched in the last 28 days, condensed nightly by
    tools/demand.refresh() so sellers' pages read one small table.
    """
    term = models.CharField(max_length=80, unique=True)
    searchers = models.PositiveIntegerField(default=0)            # found something
    no_result_searchers = models.PositiveIntegerField(default=0)  # found nothing
    live_results = models.PositiveIntegerField(default=0)         # live listings matching now
    sub_category = models.ForeignKey('product.Sub_Category', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['sub_category', '-searchers'])]

    @property
    def total(self):
        return self.searchers + self.no_result_searchers


class PriceChange(models.Model):
    """A price the repricer (or bulk edit) changed: the seller's audit trail."""
    REPRICER = 'repricer'
    BULK_EDIT = 'bulk_edit'
    API = 'api'
    SOURCE_CHOICES = [(REPRICER, 'Auto-repricer'), (BULK_EDIT, 'Bulk edit'), (API, 'Seller API')]

    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='price_changes')
    product = models.ForeignKey('product.Product', on_delete=models.CASCADE, related_name='+')
    variant = models.ForeignKey('product.Variants', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    old_price = models.DecimalField(max_digits=10, decimal_places=2)
    new_price = models.DecimalField(max_digits=10, decimal_places=2)
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES)
    note = models.CharField(max_length=160, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['vendor', '-created_at'])]

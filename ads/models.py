"""
Sponsored products.

A store runs campaigns: a set of its products, a cost-per-click bid, a daily
budget, and optionally keywords. Ads appear in the first slots of search
results (keyword or relevance match) and category pages (same sub-category).
Shoppers' clicks are billed second-price (never more than the bid) and the
day's spend is posted to the store's earnings ledger each night.
"""
from django.conf import settings
from django.db import models


class AdCampaign(models.Model):
    ACTIVE, PAUSED, ARCHIVED = 'active', 'paused', 'archived'
    STATUS_CHOICES = [(ACTIVE, 'Active'), (PAUSED, 'Paused'), (ARCHIVED, 'Archived')]

    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, related_name='ad_campaigns')
    name = models.CharField(max_length=80)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=ACTIVE)
    bid = models.DecimalField(max_digits=8, decimal_places=2, help_text="Most you pay per click (GHS).")
    daily_budget = models.DecimalField(max_digits=10, decimal_places=2)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    # Off: only listed keywords trigger search ads. On: also shown for relevant searches.
    auto_targeting = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'start_date'], name='ad_campaign_live_idx')]

    def __str__(self):
        return f"{self.name} ({self.vendor_id})"


class AdCampaignProduct(models.Model):
    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('product.Product', on_delete=models.CASCADE, related_name='ad_items')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['campaign', 'product'], name='uniq_ad_campaign_product')]


class AdKeyword(models.Model):
    PHRASE, EXACT = 'phrase', 'exact'
    MATCH_CHOICES = [(PHRASE, 'Search contains the words'), (EXACT, 'Search is exactly this')]

    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE, related_name='keywords')
    phrase = models.CharField(max_length=60, db_index=True, help_text="Lowercase, single spaces.")
    match = models.CharField(max_length=6, choices=MATCH_CHOICES, default=PHRASE)
    bid = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                              help_text="Overrides the campaign bid for this keyword.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=['campaign', 'phrase'], name='uniq_ad_keyword')]


class AdDailyStat(models.Model):
    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE, related_name='stats')
    product = models.ForeignKey('product.Product', on_delete=models.CASCADE, related_name='+')
    date = models.DateField()
    impressions = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    spend = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    orders = models.PositiveIntegerField(default=0)
    sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    billed = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['campaign', 'product', 'date'], name='uniq_ad_daily_stat')]
        indexes = [models.Index(fields=['billed', 'date'], name='ad_stat_billing_idx')]


class AdClick(models.Model):
    """One billed click. Kept 90 days for order attribution and disputes."""
    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE, related_name='clicks')
    product = models.ForeignKey('product.Product', on_delete=models.CASCADE, related_name='+')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    visitor = models.CharField(max_length=40)
    cost = models.DecimalField(max_digits=8, decimal_places=2)
    placement = models.CharField(max_length=10)        # search | category
    order_product = models.ForeignKey('order.OrderProduct', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=['user', 'product', '-created_at'], name='ad_click_attr_idx')]

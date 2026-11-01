from django.contrib import admin

from .models import AdCampaign, AdCampaignProduct, AdClick, AdDailyStat, AdKeyword


class ItemInline(admin.TabularInline):
    model = AdCampaignProduct
    raw_id_fields = ('product',)
    extra = 0


class KeywordInline(admin.TabularInline):
    model = AdKeyword
    extra = 0


@admin.register(AdCampaign)
class AdCampaignAdmin(admin.ModelAdmin):
    list_display = ('name', 'vendor', 'status', 'bid', 'daily_budget', 'start_date', 'end_date')
    list_filter = ('status',)
    search_fields = ('name', 'vendor__name')
    raw_id_fields = ('vendor',)
    inlines = [ItemInline, KeywordInline]


@admin.register(AdDailyStat)
class AdDailyStatAdmin(admin.ModelAdmin):
    list_display = ('date', 'campaign', 'product', 'impressions', 'clicks', 'spend', 'orders', 'sales', 'billed')
    list_filter = ('billed', 'date')
    raw_id_fields = ('campaign', 'product')
    date_hierarchy = 'date'


@admin.register(AdClick)
class AdClickAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'campaign', 'product', 'cost', 'placement', 'user', 'order_product')
    raw_id_fields = ('campaign', 'product', 'user', 'order_product')
    date_hierarchy = 'created_at'

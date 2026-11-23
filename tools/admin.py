from django.contrib import admin

from .models import PriceChange, RepricerRule, ToolUsage


@admin.register(ToolUsage)
class ToolUsageAdmin(admin.ModelAdmin):
    list_display = ('vendor', 'tool', 'month', 'count')
    list_filter = ('tool', 'month')
    search_fields = ('vendor__name',)
    raw_id_fields = ('vendor',)


@admin.register(RepricerRule)
class RepricerRuleAdmin(admin.ModelAdmin):
    list_display = ('product', 'vendor', 'strategy', 'min_price', 'max_price', 'is_active', 'last_run_at', 'last_result')
    list_filter = ('is_active', 'strategy')
    search_fields = ('product__title', 'vendor__name')
    raw_id_fields = ('vendor', 'product')


@admin.register(PriceChange)
class PriceChangeAdmin(admin.ModelAdmin):
    list_display = ('product', 'vendor', 'old_price', 'new_price', 'source', 'created_at')
    list_filter = ('source',)
    search_fields = ('product__title', 'vendor__name')
    raw_id_fields = ('vendor', 'product', 'variant')
    readonly_fields = [f.name for f in PriceChange._meta.fields]

    def has_add_permission(self, request):
        return False

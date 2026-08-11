"""
recommendation/admin.py

Read-mostly admin. These tables are model output, not editable content — the way
to change them is to retrain, not to type into a form. What the admin is for is
answering "why is this product ranked here?", so the views lead with the score
components and the evidence behind them.
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    ModelRun, NotInterested, ProductDealScore, ProductEmbedding, ProductNeighbor,
    ProductPriceHistory, RecommendationEvent, UserEmbedding, UserRecommendation,
)


@admin.register(ModelRun)
class ModelRunAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'status', 'started_at', 'duration_display', 'n_users', 'n_items',
        'n_interactions', 'cf_weight', 'precision_display', 'lift_display',
    )
    list_filter = ('status',)
    readonly_fields = [field.name for field in ModelRun._meta.fields]
    ordering = ('-started_at',)

    @admin.display(description='Duration')
    def duration_display(self, obj):
        return f"{obj.duration_seconds:.1f}s" if obj.duration_seconds else '—'

    @admin.display(description='Precision@10')
    def precision_display(self, obj):
        if obj.precision_at_10 is None:
            return '—'
        return f"{obj.precision_at_10:.4f}"

    @admin.display(description='vs. popularity')
    def lift_display(self, obj):
        lift = obj.lift_over_baseline
        if lift is None:
            return '—'
        colour = '#2e7d32' if lift > 0 else '#c62828'
        return format_html('<b style="color:{}">{:+.1%}</b>', colour, lift)

    def has_add_permission(self, request):
        return False


@admin.register(ProductDealScore)
class ProductDealScoreAdmin(admin.ModelAdmin):
    list_display = (
        'product', 'score', 'discount_percent', 'best_price', 'stock_remaining',
        'has_flash_sale', 'credibility_display', 'is_eligible', 'ineligible_reason',
    )
    list_filter = ('is_eligible', 'has_flash_sale')
    search_fields = ('product__title', 'product__sku')
    readonly_fields = [field.name for field in ProductDealScore._meta.fields]
    ordering = ('-score',)

    @admin.display(description='Price credibility', ordering='price_percentile')
    def credibility_display(self, obj):
        """
        How far today's price actually sits below this product's own 30-day range.
        Low values mean the advertised discount is not backed by a real price drop.
        """
        colour = '#2e7d32' if obj.price_percentile >= 0.6 else (
            '#ef6c00' if obj.price_percentile >= 0.3 else '#c62828'
        )
        return format_html('<b style="color:{}">{:.0%}</b>', colour, obj.price_percentile)

    def has_add_permission(self, request):
        return False


@admin.register(ProductNeighbor)
class ProductNeighborAdmin(admin.ModelAdmin):
    list_display = ('product', 'neighbor', 'kind', 'score', 'rank', 'support')
    list_filter = ('kind',)
    search_fields = ('product__title', 'neighbor__title')
    raw_id_fields = ('product', 'neighbor')
    ordering = ('product_id', 'kind', 'rank')

    def has_add_permission(self, request):
        return False


@admin.register(UserRecommendation)
class UserRecommendationAdmin(admin.ModelAdmin):
    list_display = ('user', 'product', 'surface', 'rank', 'score', 'reason', 'reason_detail')
    list_filter = ('surface', 'reason')
    search_fields = ('user__email', 'product__title')
    raw_id_fields = ('user', 'product', 'source_product')
    ordering = ('user_id', 'rank')

    def has_add_permission(self, request):
        return False


@admin.register(RecommendationEvent)
class RecommendationEventAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'surface', 'event_type', 'product', 'position', 'user', 'visitor_key')
    list_filter = ('surface', 'event_type', 'date')
    search_fields = ('product__title', 'visitor_key', 'user__email')
    raw_id_fields = ('user', 'product')
    date_hierarchy = 'date'

    def has_add_permission(self, request):
        return False


@admin.register(ProductEmbedding)
class ProductEmbeddingAdmin(admin.ModelAdmin):
    list_display = ('product', 'dim', 'interaction_count', 'has_cf', 'model_run', 'updated_at')
    search_fields = ('product__title', 'product__sku')
    raw_id_fields = ('product',)
    readonly_fields = ('product', 'dim', 'interaction_count', 'model_run', 'updated_at')
    exclude = ('cf_vector', 'content_vector')

    @admin.display(boolean=True, description='Has collaborative vector')
    def has_cf(self, obj):
        vector = obj.cf
        return vector is not None and bool(vector.any())

    def has_add_permission(self, request):
        return False


@admin.register(UserEmbedding)
class UserEmbeddingAdmin(admin.ModelAdmin):
    list_display = ('user', 'dim', 'interaction_count', 'is_cold_start', 'updated_at')
    list_filter = ('is_cold_start',)
    search_fields = ('user__email',)
    raw_id_fields = ('user',)
    exclude = ('cf_vector',)

    def has_add_permission(self, request):
        return False


@admin.register(ProductPriceHistory)
class ProductPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ('product', 'date', 'price', 'old_price')
    search_fields = ('product__title', 'product__sku')
    raw_id_fields = ('product',)
    date_hierarchy = 'date'


@admin.register(NotInterested)
class NotInterestedAdmin(admin.ModelAdmin):
    list_display = ('user', 'visitor_key', 'product', 'created_at')
    search_fields = ('user__email', 'product__title', 'visitor_key')
    raw_id_fields = ('user', 'product')

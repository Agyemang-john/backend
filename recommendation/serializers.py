"""
recommendation/serializers.py

Product cards for the recommendation rails.

Prices are serialised in raw GHS and converted afterwards by `apply_currency()`,
matching how core/views.py already handles this. The reason is caching: a cached
rail must be currency-agnostic, otherwise the cache fragments across every
currency a visitor might request and the hit rate collapses.
"""

from decimal import Decimal

from rest_framework import serializers

from core.service import get_exchange_rates
from product.models import Product

from .models import ProductDealScore


def apply_currency(cards: list[dict], currency: str) -> list[dict]:
    """Convert GHS prices to the requested currency, without mutating the input."""
    rates = get_exchange_rates()
    rate = Decimal(str(rates.get(currency, 1)))

    converted = []
    for card in cards:
        row = dict(card)
        row['currency'] = currency
        for field in ('price', 'old_price', 'deal_price'):
            if row.get(field) is not None:
                row[field] = round(Decimal(str(row[field])) * rate, 2)
        converted.append(row)
    return converted


class RecommendedProductSerializer(serializers.ModelSerializer):
    """
    Lightweight card — everything a rail tile needs, nothing it doesn't.

    `reason` is populated from a {product_id: text} map passed in context so the
    UI can caption each tile ("Because you viewed …"), which is what separates a
    personalised rail from an anonymous grid of products.
    """

    image = serializers.SerializerMethodField()
    average_rating = serializers.FloatField(source='avg_rating', read_only=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True, default='')
    sub_category_slug = serializers.CharField(source='sub_category.slug', read_only=True, default='')
    reason = serializers.SerializerMethodField()
    discount_percent = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'title', 'slug', 'sku', 'image', 'price', 'old_price',
            'average_rating', 'review_count', 'vendor_name', 'sub_category_slug',
            'discount_percent', 'reason',
        ]

    def get_image(self, obj):
        request = self.context.get('request')
        if not obj.image:
            return None
        try:
            url = obj.image.url
        except (ValueError, AttributeError):
            return None
        return request.build_absolute_uri(url) if request else url

    def get_reason(self, obj):
        return self.context.get('reasons', {}).get(obj.id, '')

    def get_discount_percent(self, obj):
        if obj.old_price and obj.price and obj.old_price > obj.price:
            return round(float((obj.old_price - obj.price) / obj.old_price * 100), 1)
        return 0.0


class DealCardSerializer(RecommendedProductSerializer):
    """
    A deal tile: the product card plus the live deal facts and — for anyone
    debugging a ranking — the component scores that produced its position.
    """

    deal_price = serializers.SerializerMethodField()
    savings_percent = serializers.SerializerMethodField()
    stock_remaining = serializers.SerializerMethodField()
    has_flash_sale = serializers.SerializerMethodField()
    deal_score = serializers.SerializerMethodField()

    class Meta(RecommendedProductSerializer.Meta):
        fields = RecommendedProductSerializer.Meta.fields + [
            'deal_price', 'savings_percent', 'stock_remaining', 'has_flash_sale', 'deal_score',
        ]

    def _deal(self, obj) -> ProductDealScore | None:
        return self.context.get('deal_scores', {}).get(obj.id)

    def get_deal_price(self, obj):
        deal = self._deal(obj)
        return deal.best_price if deal and deal.best_price is not None else obj.price

    def get_savings_percent(self, obj):
        deal = self._deal(obj)
        if deal:
            return round(deal.discount_percent, 1)
        return self.get_discount_percent(obj)

    def get_stock_remaining(self, obj):
        deal = self._deal(obj)
        return deal.stock_remaining if deal else None

    def get_has_flash_sale(self, obj):
        deal = self._deal(obj)
        return bool(deal.has_flash_sale) if deal else False

    def get_deal_score(self, obj):
        """Exposed only when ?debug=1 — useful when a ranking looks wrong."""
        if not self.context.get('include_debug'):
            return None
        deal = self._deal(obj)
        if not deal:
            return None
        return {
            'score': round(deal.score, 2),
            'discount': round(deal.discount_component, 4),
            'demand': round(deal.demand_component, 4),
            'quality': round(deal.quality_component, 4),
            'scarcity': round(deal.scarcity_component, 4),
            'freshness': round(deal.freshness_component, 4),
            'price_credibility': round(deal.price_percentile, 3),
        }


class TrackEventSerializer(serializers.Serializer):
    """Validates one impression/click beacon from the storefront."""

    product_id = serializers.IntegerField()
    surface = serializers.CharField(max_length=30)
    event_type = serializers.ChoiceField(choices=['impression', 'click', 'add_to_cart', 'purchase'])
    position = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=500)
    reason = serializers.CharField(max_length=30, required=False, allow_blank=True)

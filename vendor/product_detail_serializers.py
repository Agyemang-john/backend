# vendor/product_detail_serializers.py
# Serializers for the vendor product analytics/detail view.
# Read-only — no editing, only deep data aggregation.

from rest_framework import serializers
from django.db.models import Sum, Count, Avg, F, Q
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth
from django.utils import timezone
from datetime import timedelta

from product.models import (
    Product, ProductImages, Variants, ProductReview,
    Wishlist, SavedProduct, ProductDeliveryOption,
    ProductViewLog, ProductDailyStats,
)
from order.models import OrderProduct, CartItem


# ── Gallery images ─────────────────────────────────────────────────────────────
class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImages
        fields = ['id', 'images']


# ── Delivery option summary ────────────────────────────────────────────────────
class ProductDeliveryOptionDetailSerializer(serializers.ModelSerializer):
    name        = serializers.CharField(source='delivery_option.name')
    description = serializers.CharField(source='delivery_option.description')
    min_days    = serializers.IntegerField(source='delivery_option.min_days')
    max_days    = serializers.IntegerField(source='delivery_option.max_days')
    cost        = serializers.DecimalField(
        source='delivery_option.cost', max_digits=10, decimal_places=2, allow_null=True
    )
    type        = serializers.CharField(source='delivery_option.type')

    class Meta:
        model  = ProductDeliveryOption
        fields = ['id', 'name', 'description', 'min_days', 'max_days', 'cost', 'type', 'default']


# ── Variant with per-variant sales ────────────────────────────────────────────
class VariantAnalyticsSerializer(serializers.ModelSerializer):
    size_name  = serializers.SerializerMethodField()
    color_name = serializers.SerializerMethodField()
    color_code = serializers.SerializerMethodField()
    units_sold = serializers.SerializerMethodField()
    revenue    = serializers.SerializerMethodField()

    class Meta:
        model  = Variants
        fields = [
            'id', 'title', 'size_name', 'color_name', 'color_code',
            'quantity', 'price', 'image',
            'units_sold', 'revenue',
        ]

    def get_size_name(self, obj):
        return obj.size.name if obj.size else None

    def get_color_name(self, obj):
        return obj.color.name if obj.color else None

    def get_color_code(self, obj):
        return obj.color.code if obj.color else None

    def get_units_sold(self, obj):
        return (
            OrderProduct.objects
            .filter(variant=obj)
            .exclude(status='canceled')
            .aggregate(total=Sum('quantity'))['total'] or 0
        )

    def get_revenue(self, obj):
        return float(
            OrderProduct.objects
            .filter(variant=obj)
            .exclude(status='canceled')
            .aggregate(total=Sum('amount'))['total'] or 0
        )


# ── Review rating distribution ─────────────────────────────────────────────────
class RatingDistributionSerializer(serializers.Serializer):
    rating = serializers.IntegerField()
    count  = serializers.IntegerField()


# ── Main product analytics serializer ─────────────────────────────────────────
class ProductDetailAnalyticsSerializer(serializers.ModelSerializer):
    """
    Deep read-only analytics serializer for the vendor product detail page.
    All aggregations happen here so the view stays clean.
    """

    # ── Core fields ───────────────────────────────────────────────────────────
    gallery_images   = ProductImageSerializer(source='p_images', many=True, read_only=True)
    delivery_options = ProductDeliveryOptionDetailSerializer(
        source='productdeliveryoption_set', many=True, read_only=True
    )
    variants_data = VariantAnalyticsSerializer(source='variants', many=True, read_only=True)

    sub_category_title = serializers.SerializerMethodField()
    brand_title        = serializers.SerializerMethodField()

    # ── Sales KPIs ────────────────────────────────────────────────────────────
    total_revenue    = serializers.SerializerMethodField()
    total_units_sold = serializers.SerializerMethodField()
    total_orders     = serializers.SerializerMethodField()
    avg_order_value  = serializers.SerializerMethodField()
    cancellation_rate = serializers.SerializerMethodField()
    refund_rate      = serializers.SerializerMethodField()

    # ── Stock ─────────────────────────────────────────────────────────────────
    stock_by_variant = serializers.SerializerMethodField()   # list of {label, quantity, units_sold}
    total_stock      = serializers.SerializerMethodField()   # sum across all variants or total_quantity

    # ── Engagement ────────────────────────────────────────────────────────────
    wishlist_count = serializers.SerializerMethodField()
    saved_count    = serializers.SerializerMethodField()
    review_count   = serializers.SerializerMethodField()
    avg_rating     = serializers.SerializerMethodField()
    rating_distribution = serializers.SerializerMethodField()

    # ── Order status breakdown ────────────────────────────────────────────────
    order_status_breakdown = serializers.SerializerMethodField()

    # ── Sales trend (last 30 days, daily) ────────────────────────────────────
    sales_trend = serializers.SerializerMethodField()

    # ── Cart quantity (all logged-in users) ───────────────────────────────────
    total_in_carts = serializers.SerializerMethodField()

    # ── Last activity timestamp ───────────────────────────────────────────────
    last_activity = serializers.SerializerMethodField()

    # ── View analytics ───────────────────────────────────────────────────────
    view_analytics = serializers.SerializerMethodField()

    class Meta:
        model  = Product
        fields = [
            # Identity
            'id', 'title', 'slug', 'sku', 'status',
            'image', 'video',
            'price', 'old_price',
            'product_type', 'variant',
            'weight', 'volume', 'life',
            'mfd', 'return_period_days', 'warranty_period_days',
            'total_quantity',
            'date', 'updated', 'views',
            'trending_score', 'deals_of_the_day', 'recommended_for_you', 'popular_product',
            'sub_category_title', 'brand_title',

            # Related
            'gallery_images',
            'delivery_options',
            'variants_data',

            # Sales KPIs
            'total_revenue', 'total_units_sold', 'total_orders',
            'avg_order_value', 'cancellation_rate', 'refund_rate',

            # Stock
            'stock_by_variant', 'total_stock',

            # Engagement
            'wishlist_count', 'saved_count', 'review_count',
            'avg_rating', 'rating_distribution',
            'total_in_carts',

            # Charts
            'order_status_breakdown',
            'sales_trend',

            # View analytics
            'view_analytics',

            # Last activity
            'last_activity',
        ]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ops(self, obj):
        """All OrderProduct records for this product."""
        return OrderProduct.objects.filter(product=obj)

    def _active_ops(self, obj):
        return self._ops(obj).exclude(status='canceled')

    # ── Core ─────────────────────────────────────────────────────────────────

    def get_sub_category_title(self, obj):
        return obj.sub_category.title if obj.sub_category else None

    def get_brand_title(self, obj):
        return obj.brand.title if obj.brand else None

    # ── Sales KPIs ────────────────────────────────────────────────────────────

    def get_total_revenue(self, obj):
        return float(self._active_ops(obj).aggregate(t=Sum('amount'))['t'] or 0)

    def get_total_units_sold(self, obj):
        return self._active_ops(obj).aggregate(t=Sum('quantity'))['t'] or 0

    def get_total_orders(self, obj):
        return self._active_ops(obj).values('order').distinct().count()

    def get_avg_order_value(self, obj):
        ops = self._active_ops(obj)
        count = ops.values('order').distinct().count()
        if not count:
            return 0
        revenue = float(ops.aggregate(t=Sum('amount'))['t'] or 0)
        return round(revenue / count, 2)

    def get_cancellation_rate(self, obj):
        total = self._ops(obj).count()
        if not total:
            return 0
        canceled = self._ops(obj).filter(status='canceled').count()
        return round(canceled / total * 100, 1)

    def get_refund_rate(self, obj):
        total = self._ops(obj).count()
        if not total:
            return 0
        refunded = self._ops(obj).filter(refund_reason__isnull=False).count()
        return round(refunded / total * 100, 1)

    # ── Stock ─────────────────────────────────────────────────────────────────

    def get_stock_by_variant(self, obj):
        """
        Returns a list of stock entries.
        For 'None' variant: single entry with product total_quantity.
        For variant products: one entry per variant with name, qty, units sold.
        """
        if obj.variant == 'None':
            return [{
                'label':      'Total stock',
                'quantity':   obj.total_quantity or 0,
                'units_sold': self.get_total_units_sold(obj),
                'color_code': None,
            }]

        result = []
        for v in obj.variants.select_related('size', 'color').all():
            units_sold = (
                OrderProduct.objects
                .filter(variant=v)
                .exclude(status='canceled')
                .aggregate(t=Sum('quantity'))['t'] or 0
            )
            parts = []
            if v.size:  parts.append(v.size.name)
            if v.color: parts.append(v.color.name)
            result.append({
                'label':      ' / '.join(parts) if parts else f'Variant {v.id}',
                'quantity':   v.quantity,
                'units_sold': units_sold,
                'color_code': v.color.code if v.color else None,
            })
        return result

    def get_total_stock(self, obj):
        if obj.variant == 'None':
            return obj.total_quantity or 0
        return obj.variants.aggregate(t=Sum('quantity'))['t'] or 0

    # ── Engagement ────────────────────────────────────────────────────────────

    def get_wishlist_count(self, obj):
        return Wishlist.objects.filter(product=obj).count()

    def get_saved_count(self, obj):
        return SavedProduct.objects.filter(product=obj).count()

    def get_total_in_carts(self, obj):
        return CartItem.objects.filter(product=obj).aggregate(total=Sum('quantity'))['total'] or 0

    def get_last_activity(self, obj):
        from django.utils import timezone as tz
        candidates = []

        if obj.updated:
            candidates.append(obj.updated)

        last_order = OrderProduct.objects.filter(product=obj).order_by('-order__date_created').values_list('order__date_created', flat=True).first()
        if last_order:
            candidates.append(last_order)

        last_review = ProductReview.objects.filter(product=obj).order_by('-date').values_list('date', flat=True).first()
        if last_review:
            candidates.append(last_review)

        last_view = ProductViewLog.objects.filter(product=obj).order_by('-viewed_at').values_list('viewed_at', flat=True).first()
        if last_view:
            candidates.append(last_view)

        if not candidates:
            return None
        return max(candidates).isoformat()

    def get_review_count(self, obj):
        return ProductReview.objects.filter(product=obj, status=True).count()

    def get_avg_rating(self, obj):
        avg = ProductReview.objects.filter(product=obj, status=True).aggregate(a=Avg('rating'))['a']
        return round(float(avg), 2) if avg else 0

    def get_rating_distribution(self, obj):
        """Returns [{rating: 1, count: 3}, ..., {rating: 5, count: 12}]"""
        dist = (
            ProductReview.objects
            .filter(product=obj, status=True)
            .values('rating')
            .annotate(count=Count('id'))
            .order_by('rating')
        )
        # Ensure all 5 ratings present
        dist_map = {d['rating']: d['count'] for d in dist}
        return [{'rating': r, 'count': dist_map.get(r, 0)} for r in range(1, 6)]

    # ── Order status breakdown ────────────────────────────────────────────────

    def get_order_status_breakdown(self, obj):
        return list(
            self._ops(obj)
            .values('status')
            .annotate(count=Count('id'))
            .order_by('status')
        )

    # ── Sales trend ───────────────────────────────────────────────────────────

    def get_sales_trend(self, obj):
        """Last 30 days of daily revenue + unit sales for this product."""
        since = timezone.now() - timedelta(days=30)
        trend = (
            self._active_ops(obj)
            .filter(order__date_created__gte=since)
            .annotate(date=TruncDate('order__date_created'))
            .values('date')
            .annotate(revenue=Sum('amount'), units=Sum('quantity'))
            .order_by('date')
        )
        return [
            {
                'date':    item['date'].isoformat(),
                'revenue': float(item['revenue'] or 0),
                'units':   item['units'] or 0,
            }
            for item in trend
        ]

    def get_view_analytics(self, obj):
        """
        Returns view statistics at four granularities plus a 30-day daily
        trend. Aggregated rows (ProductDailyStats) are used for speed on
        historical days; today's live rows come from ProductViewLog directly.
        Bot views are reported separately and excluded from the non-bot totals.
        """
        from datetime import date as _date

        today = timezone.now().date()
        week_start  = today - timedelta(days=6)
        month_start = today - timedelta(days=29)

        def _sum_logs(qs):
            agg = qs.aggregate(
                total=Count('id'),
                unique=Count('id', filter=Q(is_returning=False, is_bot=False)),
                returning=Count('id', filter=Q(is_returning=True, is_bot=False)),
                bots=Count('id', filter=Q(is_bot=True)),
            )
            return {
                'total':     (agg['total'] or 0) - (agg['bots'] or 0),
                'unique':    agg['unique'] or 0,
                'returning': agg['returning'] or 0,
                'bots':      agg['bots'] or 0,
            }

        def _sum_stats(qs):
            agg = qs.aggregate(
                total=Sum('total_views'),
                unique=Sum('unique_views'),
                returning=Sum('returning_views'),
                bots=Sum('bot_views'),
            )
            total = agg['total'] or 0
            bots  = agg['bots'] or 0
            return {
                'total':     total - bots,   # non-bot total, matches _sum_logs
                'unique':    agg['unique'] or 0,
                'returning': agg['returning'] or 0,
                'bots':      bots,
            }

        base_logs  = ProductViewLog.objects.filter(product=obj)
        base_stats = ProductDailyStats.objects.filter(product=obj)

        today_data  = _sum_logs(base_logs.filter(date=today))
        week_data   = _combine(_sum_stats(base_stats.filter(date__range=(week_start, today - timedelta(days=1)))), today_data)
        month_data  = _combine(_sum_stats(base_stats.filter(date__range=(month_start, today - timedelta(days=1)))), today_data)

        all_time_non_bot = obj.views  # already bot-filtered in buffer_view_count

        # 30-day daily trend: use aggregated stats for past days, live for today
        daily_stats = {
            row['date']: row
            for row in base_stats.filter(date__range=(month_start, today - timedelta(days=1))).values(
                'date', 'total_views', 'unique_views', 'returning_views', 'bot_views'
            )
        }
        trend = []
        for i in range(29, -1, -1):
            d = today - timedelta(days=i)
            if d == today:
                row = today_data
                trend.append({
                    'date':      d.isoformat(),
                    'total':     row['total'],
                    'unique':    row['unique'],
                    'returning': row['returning'],
                    'bots':      row['bots'],
                })
            elif d in daily_stats:
                s = daily_stats[d]
                trend.append({
                    'date':      d.isoformat(),
                    'total':     s['total_views'] - s['bot_views'],
                    'unique':    s['unique_views'],
                    'returning': s['returning_views'],
                    'bots':      s['bot_views'],
                })
            else:
                trend.append({'date': d.isoformat(), 'total': 0, 'unique': 0, 'returning': 0, 'bots': 0})

        return {
            'today':    today_data,
            'week':     week_data,
            'month':    month_data,
            'all_time': {'total': all_time_non_bot},
            'trend':    trend,
        }


def _combine(stats_row: dict, today_row: dict) -> dict:
    """Merge an aggregated historical dict with today's live dict."""
    return {
        'total':     stats_row['total']     + today_row['total'],
        'unique':    stats_row['unique']    + today_row['unique'],
        'returning': stats_row['returning'] + today_row['returning'],
        'bots':      stats_row['bots']      + today_row['bots'],
    }
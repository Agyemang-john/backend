from rest_framework import serializers

class SalesSummarySerializer(serializers.Serializer):
    total_revenue = serializers.FloatField()
    total_orders = serializers.IntegerField()
    total_units_sold = serializers.IntegerField()
    avg_order_value = serializers.FloatField()
    cancellation_rate = serializers.FloatField()
    refund_rate = serializers.FloatField()
    on_time_delivery_rate = serializers.FloatField()
    avg_rating = serializers.FloatField()
    total_views = serializers.IntegerField()
    wishlist_count = serializers.IntegerField()

class SalesTrendSerializer(serializers.Serializer):
    date = serializers.DateField()
    revenue = serializers.FloatField()
    orders = serializers.IntegerField()

class TopProductSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(source='product__id')
    title = serializers.CharField(source='product__title')
    revenue = serializers.FloatField()
    units_sold = serializers.IntegerField()

class OrderStatusSerializer(serializers.Serializer):
    status = serializers.CharField()
    count = serializers.IntegerField()

class EngagementSerializer(serializers.Serializer):
    total_views = serializers.IntegerField()
    wishlist_count = serializers.IntegerField()
    saved_count = serializers.IntegerField()
    review_count = serializers.IntegerField()
    avg_rating = serializers.FloatField()

class DeliveryPerformanceSerializer(serializers.Serializer):
    on_time_delivery_rate = serializers.FloatField()
    total_delivered = serializers.IntegerField()
    overdue_deliveries = serializers.IntegerField()
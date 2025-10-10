from rest_framework import serializers
from django.contrib.auth import get_user_model
from order.models import Order, OrderProduct
from product.models import Product, Variants, DeliveryOption
from address.models import Address
import logging
from decimal import Decimal

User = get_user_model()
logger = logging.getLogger(__name__)

class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = ['full_name', 'country', 'region', 'town', 'address', 'gps_address', 'email', 'mobile']

class DeliveryOptionSerializer(serializers.ModelSerializer):
    delivery_range = serializers.CharField(source='get_delivery_date_range', read_only=True)
    delivery_status = serializers.CharField(source='get_delivery_status', read_only=True)

    class Meta:
        model = DeliveryOption
        fields = ['name', 'cost', 'min_days', 'max_days', 'delivery_range', 'delivery_status']

class VariantsSerializer(serializers.ModelSerializer):
    size_name = serializers.CharField(source='size.name', allow_null=True)
    color_name = serializers.CharField(source='color.name', allow_null=True)

    class Meta:
        model = Variants
        fields = ['title', 'image', 'size_name', 'color_name', 'price']

class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ['title', 'image']

class OrderProductSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    variant = VariantsSerializer(allow_null=True)
    delivery_date_range = serializers.SerializerMethodField()
    delivery_status = serializers.SerializerMethodField()
    selected_delivery_option = DeliveryOptionSerializer(allow_null=True)

    class Meta:
        model = OrderProduct
        fields = [
            'id',
            'product',
            'variant',
            'quantity',
            'price',
            'amount',
            'status',
            'delivery_date_range',
            'delivery_status',
            'selected_delivery_option',
        ]

    def get_delivery_date_range(self, obj):
        try:
            return obj.get_delivery_range()
        except Exception as e:
            logger.error(f"Error getting delivery range for OrderProduct {obj.id}: {str(e)}")
            return None

    def get_delivery_status(self, obj):
        try:
            return obj.get_delivery_status()
        except Exception as e:
            logger.error(f"Error getting delivery status for OrderProduct {obj.id}: {str(e)}")
            return "Delivery status unavailable"

class VendorOrderSerializer(serializers.ModelSerializer):
    grand_total = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email', read_only=True, allow_null=True)
    vendor_delivery_date_range = serializers.SerializerMethodField()
    vendor_delivery_status = serializers.SerializerMethodField()
    vendor_delivery_fee = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'order_number',
            'date_created',
            'grand_total',
            'status',
            'payment_method',
            'user_email',
            'vendor_delivery_date_range',
            'vendor_delivery_status',
            'vendor_delivery_fee',
        ]
        read_only_fields = ['id', 'order_number', 'date_created', 'grand_total', 'status']

    def get_vendor_delivery_fee(self, obj):
        vendor = self.context.get('vendor')
        try:
            if vendor:
                fee_result = obj.calculate_vendor_delivery_fee(vendor)
                return fee_result.total  # Extract the total from FeeResult
            return Decimal(0)
        except Exception as e:
            logger.error(f"Error calculating vendor delivery fee for Order {obj.order_number}: {str(e)}")
            return Decimal(0)

    def get_grand_total(self, obj):
        vendor = self.context.get('vendor')
        try:
            if vendor:
                return obj.calculate_vendor_grand_total(vendor)
            return obj.total  # Fallback for non-vendor context
        except Exception as e:
            logger.error(f"Error calculating grand_total for Order {obj.order_number}: {str(e)}")
            return obj.total

    def get_vendor_delivery_date_range(self, obj):
        vendor = self.context.get('vendor')
        try:
            return obj.get_vendor_delivery_date_range(vendor)
        except Exception as e:
            logger.error(f"Error getting vendor delivery range for Order {obj.order_number}: {str(e)}")
            return None

    def get_vendor_delivery_status(self, obj):
        vendor = self.context.get('vendor')
        try:
            order_products = obj.order_products.filter(product__vendor=vendor)
            if not order_products.exists():
                return "No products"
            statuses = {product.get_delivery_status() for product in order_products}
            if len(statuses) == 1:
                return statuses.pop()
            if "OVERDUE" in statuses:
                return "OVERDUE"
            if "ONGOING" in statuses:
                return "ONGOING"
            if any(status.startswith("IN ") for status in statuses):
                days = [
                    int(status.split("IN ")[1].split(" DAYS")[0])
                    for status in statuses if status.startswith("IN ")
                ]
                return f"IN {min(days)} DAYS" if days else "UPCOMING"
            return "UPCOMING"
        except Exception as e:
            logger.error(f"Error getting vendor delivery status for Order {obj.order_number}: {str(e)}")
            return "Delivery status unavailable"

class OrderSerializer(serializers.ModelSerializer):
    order_products = serializers.SerializerMethodField()
    address = AddressSerializer()
    vendor_delivery_date_range = serializers.SerializerMethodField()
    vendor_total = serializers.SerializerMethodField()
    vendor_delivery_cost = serializers.SerializerMethodField()
    vendor_delivery_status = serializers.SerializerMethodField()
    grand_total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'order_number',
            'address',
            'status',
            'total',
            'payment_method',
            'date_created',
            'order_products',
            'vendor_delivery_date_range',
            'vendor_total',
            'vendor_delivery_cost',
            'vendor_delivery_status',
            'grand_total',
        ]

    def get_order_products(self, obj):
        vendor = self.context.get('vendor')
        request = self.context.get('request')
        try:
            order_products = OrderProduct.objects.filter(
                order=obj, product__vendor=vendor
            ).select_related(
                'product', 'variant', 'selected_delivery_option'
            ).prefetch_related(
                'product__vendor',
                'variant__size',
                'variant__color'
            )
            return OrderProductSerializer(order_products, many=True, context={'request': request}).data
        except Exception as e:
            logger.error(f"Error getting order products for Order {obj.order_number} and vendor {vendor.id if vendor else 'no vendor'}: {str(e)}")
            return []

    def get_vendor_delivery_date_range(self, obj):
        vendor = self.context.get('vendor')
        try:
            return obj.get_vendor_delivery_date_range(vendor)
        except Exception as e:
            logger.error(f"Error getting vendor delivery range for Order {obj.order_number}: {str(e)}")
            return None

    def get_vendor_total(self, obj):
        vendor = self.context.get('vendor')
        try:
            return obj.get_vendor_total(vendor) if vendor else obj.total_price
        except Exception as e:
            logger.error(f"Error getting vendor total for Order {obj.order_number}: {str(e)}")
            return Decimal(0)

    def get_vendor_delivery_cost(self, obj):
        vendor = self.context.get('vendor')
        try:
            if vendor:
                fee_result = obj.calculate_vendor_delivery_fee(vendor)
                return fee_result.total
            else:
                return obj.calculate_total_delivery_fee()
        except Exception as e:
            logger.error(f"Error getting vendor delivery cost for Order {obj.order_number}: {str(e)}")
            return Decimal(0)

    def get_vendor_delivery_status(self, obj):
        vendor = self.context.get('vendor')
        try:
            order_products = obj.order_products.filter(product__vendor=vendor)
            if not order_products.exists():
                return "No products"
            statuses = {product.get_delivery_status() for product in order_products}
            if len(statuses) == 1:
                return statuses.pop()
            if "OVERDUE" in statuses:
                return "OVERDUE"
            if "ONGOING" in statuses:
                return "ONGOING"
            if any(status.startswith("IN ") for status in statuses):
                days = [
                    int(status.split("IN ")[1].split(" DAYS")[0])
                    for status in statuses if status.startswith("IN ")
                ]
                return f"IN {min(days)} DAYS" if days else "UPCOMING"
            return "UPCOMING"
        except Exception as e:
            logger.error(f"Error getting vendor delivery status for Order {obj.order_number}: {str(e)}")
            return "Delivery status unavailable"

    def get_grand_total(self, obj):
        vendor = self.context.get('vendor')
        try:
            if vendor:
                return obj.calculate_vendor_grand_total(vendor)
            else:
                return obj.calculate_grand_total()
        except Exception as e:
            logger.error(f"Error calculating grand_total for Order {obj.order_number}: {str(e)}")
            return obj.total

from rest_framework import serializers
from order.models import Cart, CartItem, Coupon, OrderProduct, ProductDeliveryOption, Order
from product.models import *
from rest_framework import serializers
from .models import DeliveryOption  # Adjust the import according to your project structure
from vendor.models import Vendor
from address.serializers import AddressSerializer
from core.service import get_exchange_rates
from decimal import Decimal



class DeliveryOptionSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    date_range = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    cost = serializers.SerializerMethodField()  # <-- Add this line

    class Meta:
        model = DeliveryOption
        fields = ['id', 'name', 'description', 'min_days', 'max_days', 'cost', 'status', 'currency', 'date_range']

    def get_status(self, obj):
        return obj.get_delivery_status()

    def get_date_range(self, obj):
        result = obj.get_delivery_date_range()
        if isinstance(result, tuple):
            return {
                "from": result[0].strftime("%Y-%m-%d"),
                "to": result[1].strftime("%Y-%m-%d")
            }
        return result
    
    def get_currency(self, obj):
        request = self.context.get('request')
        return request.headers.get('X-Currency', 'GHS') if request else 'GHS'

    def get_cost(self, obj):
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        rates = get_exchange_rates()  # Make sure this is imported and working

        exchange_rate = Decimal(rates.get(currency, 1))  # Default to 1 if currency not found
        return round(obj.cost * exchange_rate, 2)

class ProductDeliveryOptionSerializer(serializers.ModelSerializer):
    delivery_option = DeliveryOptionSerializer()
    

    class Meta:
        model = ProductDeliveryOption
        fields = '__all__'

    def get_delivery_date_range(self, obj):
        now = datetime.now()
        if (obj.delivery_option.name.lower() == "same-day delivery" or 
            obj.delivery_option.name.lower() == "same-day" and now.hour >= 10):
            return 'Tomorrow'
        elif (obj.delivery_option.name.lower() == "same-day delivery" or 
              obj.delivery_option.name.lower() == "same-day" and now.hour <= 9):
            return 'Today'

        min_delivery_date = now + timedelta(days=obj.delivery_option.min_days)
        max_delivery_date = now + timedelta(days=obj.delivery_option.max_days)
        return f"{min_delivery_date.strftime('%d %B')} to {max_delivery_date.strftime('%d %B')}"

class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = ['name']

class ProductSerializer(serializers.ModelSerializer):
    delivery_options = DeliveryOptionSerializer(many=True)
    vendor = VendorSerializer()
    currency = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    
    class Meta:
        model = Product
        fields = [
            "id",
            "slug",
            "vendor",
            "variant",
            "status",
            "title",
            "image",
            "price",             # Now handled by get_price
            "features",
            "description",
            "specifications",
            "delivery_returns",
            "available_in_regions",
            "product_type",
            "total_quantity",
            "weight",
            "volume",
            "life",
            "mfd",
            "delivery_options",
            "sku",
            "date",
            "updated",
            "views",
            "currency",
        ]
    
    def get_currency(self, obj):
        request = self.context.get('request')
        return request.headers.get('X-Currency', 'GHS') if request else 'GHS'

    def get_price(self, obj):
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        if currency:
            rates = get_exchange_rates()
            return round(obj.price * rates.get(currency, 1), 2)
        return obj.price

class ColorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Color
        fields = '__all__'

class SizeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Size
        fields = '__all__'

class VariantSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    size = SizeSerializer()
    color = ColorSerializer()
    currency = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()

    class Meta:
        model = Variants
        fields = [
            "product", "price", "title", "color", "size", "sku",
            "quantity", "image", "currency", "id"
        ]

    def get_currency(self, obj):
        request = self.context.get('request')
        return request.headers.get('X-Currency', 'GHS') if request else 'GHS'

    def get_price(self, obj):
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        rates = get_exchange_rates()  # Make sure this is imported and working

        exchange_rate = rates.get(currency, 1)  # Default to 1 if currency not found
        return round(obj.price * exchange_rate, 2)   

class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    variant = VariantSerializer(required=False)
    item_total = serializers.SerializerMethodField()
    packaging_fee = serializers.SerializerMethodField()
    delivery_option = DeliveryOptionSerializer()

    class Meta:
        model = CartItem
        fields = ['product', 'variant', 'quantity', 'item_total', 'packaging_fee', 'delivery_option']
    
    def get_item_total(self, obj):
        return obj.amount
    
    def get_packaging_fee(self, obj):
        return obj.packaging_fee()

class CartSerializer(serializers.ModelSerializer):
    cart_items = CartItemSerializer(many=True)

    class Meta:
        model = Cart
        fields = '__all__'



class CouponSerializer(serializers.ModelSerializer):
    is_valid = serializers.SerializerMethodField()

    class Meta:
        model = Coupon
        fields = ['code', 'discount_amount', 'discount_percentage', 'valid_from', 'valid_to', 'active', 'max_uses', 'used_count', 'min_purchase_amount', 'is_valid']

    # Custom method to return the validity status of the coupon
    def get_is_valid(self, obj):
        return obj.is_valid()

class OrderProductSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source='product.title', read_only=True)
    variant_title = serializers.CharField(source='variant.title', read_only=True)
    delivery_range = serializers.SerializerMethodField()

    class Meta:
        model = OrderProduct
        fields = [
            'id',
            'product_title',
            'variant_title',
            'quantity',
            'price',
            'amount',
            'status',
            'delivery_range',
        ]

    def get_delivery_range(self, obj):
        return obj.get_delivery_range()

class OrderSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    address = AddressSerializer()
    order_products = OrderProductSerializer(many=True)
    overall_delivery_range = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'order_number',
            'payment_method',
            'total',
            'status',
            'is_ordered',
            'date_created',
            'address',
            'user',
            'order_products',
            'overall_delivery_range',
        ]

    def get_user(self, obj):
        return {
            'first_name': obj.user.first_name,
            'last_name': obj.user.last_name,
            'email': obj.user.email
        }

    def get_overall_delivery_range(self, obj):
        return obj.get_overall_delivery_range()
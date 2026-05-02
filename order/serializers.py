from rest_framework import serializers
from order.models import Cart, CartItem, Coupon, OrderProduct, ProductDeliveryOption, Order, Shipment, TrackingEvent
from product.models import *
from rest_framework import serializers
from .models import DeliveryOption  # Adjust the import according to your project structure
from vendor.models import Vendor
from address.serializers import AddressSerializer
from core.service import get_exchange_rates
from decimal import Decimal
from datetime import datetime, timedelta

class DeliveryOptionSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    date_range = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    cost = serializers.SerializerMethodField()  # <-- Add this line

    class Meta:
        model = DeliveryOption
        fields = ['id', 'name', 'description', 'min_days', 'max_days', 'cost', 'type', 'provider', 'currency', 'date_range', 'status']

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

        exchange_rate = Decimal(str(rates.get(currency, 1)))  # Default to 1 if currency not found
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
    shipping_from_country = serializers.CharField(source="shipping_from_country.name", read_only=True)
    class Meta:
        model = Vendor
        fields = ['name', 'shipping_from_country']

class ProductSerializer(serializers.ModelSerializer):
    delivery_options = DeliveryOptionSerializer(many=True)
    vendor = VendorSerializer()
    currency = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()

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
            "price",
            "old_price",
            "return_period_days",
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
            exchange_rate = Decimal(str(rates.get(currency, 1)))
            return round(obj.price * exchange_rate, 2)
        return obj.price

    def get_old_price(self, obj):
        if not obj.old_price:
            return None
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))
        return round(obj.old_price * exchange_rate, 2)

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
            "product", "price", "title", "color", "size",
            "quantity", "image", "currency", "id"
        ]

    def get_currency(self, obj):
        request = self.context.get('request')
        return request.headers.get('X-Currency', 'GHS') if request else 'GHS'

    def get_price(self, obj):
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        rates = get_exchange_rates()  # Make sure this is imported and working

        exchange_rate = Decimal(str(rates.get(currency, 1)))  # Default to 1 if currency not found
        return round(obj.price * exchange_rate, 2)   

class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    variant = VariantSerializer(required=False)
    item_total = serializers.SerializerMethodField()
    packaging_fee = serializers.SerializerMethodField()
    delivery_option = DeliveryOptionSerializer()
    effective_unit_price = serializers.SerializerMethodField()
    is_flash_sale = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = [
            'id', 'product', 'variant', 'quantity',
            'item_total', 'packaging_fee', 'delivery_option',
            'flash_sale_price', 'effective_unit_price', 'is_flash_sale',
        ]

    def _rate(self):
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        rates = get_exchange_rates()
        return Decimal(str(rates.get(currency, 1)))

    def get_item_total(self, obj):
        return round(obj.amount * self._rate(), 2)

    def get_effective_unit_price(self, obj):
        return round(obj.price * self._rate(), 2)

    def get_is_flash_sale(self, obj):
        return obj.flash_sale_price is not None

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
    product_title = serializers.SerializerMethodField()
    product_image = serializers.SerializerMethodField()
    product_slug = serializers.SerializerMethodField()
    product_sku = serializers.SerializerMethodField()
    variant_title = serializers.SerializerMethodField()
    variant_image = serializers.SerializerMethodField()
    variant_size = serializers.SerializerMethodField()
    variant_color = serializers.SerializerMethodField()
    delivery_option_name = serializers.SerializerMethodField()
    delivery_range = serializers.SerializerMethodField()

    class Meta:
        model = OrderProduct
        fields = [
            'id',
            'product_title',
            'product_image',
            'product_slug',
            'product_sku',
            'variant_title',
            'variant_image',
            'variant_size',
            'variant_color',
            'quantity',
            'price',
            'amount',
            'status',
            'delivery_range',
            'delivery_option_name',
        ]

    def get_product_title(self, obj):
        if obj.product:
            return obj.product.title
        return "Product no longer available"

    def get_product_slug(self, obj):
        return obj.product.slug if obj.product else None

    def get_product_sku(self, obj):
        return obj.product.sku if obj.product else None

    def get_product_image(self, obj):
        request = self.context.get('request')
        if obj.product and obj.product.image:
            try:
                url = obj.product.image.url
                return request.build_absolute_uri(url) if request else url
            except Exception:
                return None
        return None

    def get_variant_title(self, obj):
        return obj.variant.title if obj.variant else None

    def get_variant_image(self, obj):
        request = self.context.get('request')
        if obj.variant and obj.variant.image:
            try:
                url = obj.variant.image.url
                return request.build_absolute_uri(url) if request else url
            except Exception:
                return None
        return None

    def get_variant_size(self, obj):
        if obj.variant and obj.variant.size:
            return obj.variant.size.name
        return None

    def get_variant_color(self, obj):
        if obj.variant and obj.variant.color:
            return obj.variant.color.name
        return None

    def get_delivery_option_name(self, obj):
        if obj.selected_delivery_option:
            return obj.selected_delivery_option.name
        return None

    def get_delivery_range(self, obj):
        return obj.get_delivery_range()

class OrderSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    address = AddressSerializer(allow_null=True)
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


class TrackingEventSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = TrackingEvent
        fields = [
            'id', 'status', 'status_display', 'description',
            'location', 'city', 'country', 'event_date', 'created_at',
        ]


class ShipmentSerializer(serializers.ModelSerializer):
    tracking_events = TrackingEventSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    latest_event = TrackingEventSerializer(read_only=True)
    items_summary = serializers.SerializerMethodField()

    class Meta:
        model = Shipment
        fields = [
            'shipment_id', 'carrier', 'carrier_code', 'tracking_number', 'tracking_url',
            'status', 'status_display', 'vendor_name', 'shipped_at', 'delivered_at',
            'estimated_delivery_date', 'progress_percentage', 'is_international',
            'tracking_events', 'latest_event', 'items_summary', 'created_at',
        ]

    def get_items_summary(self, obj):
        return [
            {
                'product_title': op.product.title,
                'quantity': op.quantity,
                'image': op.product.image.url if op.product.image else None,
            }
            for op in obj.items.select_related('product').all()
        ]


class OrderTrackingSerializer(serializers.ModelSerializer):
    shipments = ShipmentSerializer(many=True, read_only=True)
    overall_status = serializers.SerializerMethodField()
    address_summary = serializers.SerializerMethodField()
    vendors_summary = serializers.SerializerMethodField()
    delivery_progress = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'status', 'date_created',
            'overall_status', 'address_summary', 'shipments',
            'vendors_summary', 'delivery_progress',
        ]

    def get_overall_status(self, obj):
        shipments = list(obj.shipments.all())
        if not shipments:
            return obj.status
        statuses = [s.status for s in shipments]
        if all(s == 'delivered' for s in statuses):
            return 'delivered'
        if any(s == 'delivered' for s in statuses):
            return 'partially_delivered'
        if any(s == 'out_for_delivery' for s in statuses):
            return 'out_for_delivery'
        if any(s in ('in_transit', 'label_created') for s in statuses):
            return 'in_transit'
        return obj.status

    def get_vendors_summary(self, obj):
        """
        Returns per-vendor shipping status so the UI can show a card for every
        seller — including those who haven't shipped yet.
        """
        # All vendors on this order (prefetched by the view/consumer)
        all_vendors = list(obj.vendors.all())
        # Shipments indexed by vendor id
        shipment_map = {s.vendor_id: s for s in obj.shipments.all()}

        result = []
        for vendor in all_vendors:
            shipment = shipment_map.get(vendor.id)
            result.append({
                'vendor_id': vendor.id,
                'vendor_name': vendor.name,
                'has_shipped': shipment is not None,
                'shipment_id': shipment.shipment_id if shipment else None,
                'shipment_status': shipment.status if shipment else 'pending',
                'carrier': shipment.carrier if shipment else None,
                'tracking_number': shipment.tracking_number if shipment else None,
                'estimated_delivery_date': str(shipment.estimated_delivery_date) if shipment and shipment.estimated_delivery_date else None,
            })
        return result

    def get_delivery_progress(self, obj):
        """
        Returns a simple progress summary: how many vendor shipments have been
        delivered out of the total number of vendors on this order.
        """
        all_vendors = obj.vendors.all()
        total = all_vendors.count()
        if total == 0:
            return {'total': 0, 'delivered': 0, 'shipped': 0, 'pending': 0}

        shipments = list(obj.shipments.all())
        delivered = sum(1 for s in shipments if s.status == 'delivered')
        shipped = sum(1 for s in shipments if s.status in ('in_transit', 'out_for_delivery', 'label_created'))
        pending = total - len(shipments)
        return {
            'total': total,
            'delivered': delivered,
            'shipped': shipped,
            'pending': pending,
        }

    def get_address_summary(self, obj):
        addr = obj.address
        if not addr:
            return None
        return {
            'full_name': addr.full_name,
            'address': addr.address,
            'town': addr.town,
            'region': addr.region,
            'country': addr.country,
        }
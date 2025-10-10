from rest_framework import serializers
from product.models import  *
from order.models import *
from django.contrib.auth import get_user_model
from django.db.models.query_utils import Q
from address.models import *
from core.service import get_exchange_rates


User = get_user_model()

from .models import OpeningHour

# Serializer for the OpeningHour model
class OpeningHourSerializer(serializers.ModelSerializer):
    day_display = serializers.CharField(source='get_day_display', read_only=True)

    class Meta:
        model = OpeningHour
        fields = ['id', 'vendor', 'day', 'day_display', 'from_hour', 'to_hour', 'is_closed']
        read_only_fields = ['vendor', 'id']

class SubCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Sub_Category
        fields = '__all__'

class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = '__all__'

class CountrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = '__all__'
        

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id','first_name', 'last_name', 'email', 'phone']

class ProductReviewSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)  # Use StringRelatedField to display the user, but make it read-only
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())  # Handle product as a related field
    product_image = serializers.SerializerMethodField()
    

    class Meta:
        model = ProductReview
        fields = ['id','review', 'rating', 'product', 'user', 'date', 'product_image']  # Include 'user' as read-only
        extra_kwargs = {'user': {'read_only': True}}
    
    def get_product_image(self, obj):
        # Access the image field from the related Product instance
        return obj.product.image.url if obj.product.image else None
    
    
class ColorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Color
        fields = ['id', 'name', 'code']

class SizeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Size
        fields = ['id', 'name', 'code']

class DeliveryOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryOption
        fields = ['id', 'name', 'description', 'min_days', 'max_days', 'cost']

class ProductImagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImages
        fields = ['id', 'images']

class VariantImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = VariantImage
        fields = ['id', 'images']
    
class VariantsSerializer(serializers.ModelSerializer):
    color = ColorSerializer(allow_null=True)
    size = SizeSerializer(allow_null=True)

    class Meta:
        model = Variants
        fields = ['id', 'title', 'size', 'color', 'image', 'quantity', 'price']


class CachedProductSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(required=False, allow_null=True)
    variants = VariantsSerializer(many=True, required=False)

    class Meta:
        model = Product
        fields = ['id', 'slug', 'sub_category', 'variant', 'title', 'image', 'sku', 'variants']

class ProductSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(required=False, allow_null=True)
    variants = VariantsSerializer(many=True, required=False)
    currency = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ['id', 'slug', 'sub_category', 'variant', 'title', 'image', 'price', 'old_price', 'sku', 'currency', 'variants', 'date']

    def get_currency(self, obj):
        request = self.context.get('request')
        return request.headers.get('X-Currency', 'GHS') if request else 'GHS'

    def get_price(self, obj):
        return self._get_converted_price(obj.price, self.context.get('request'))

    def get_old_price(self, obj):
        return self._get_converted_price(obj.old_price, self.context.get('request')) if obj.old_price else None

    def _get_converted_price(self, price, request):
        if not price:
            return None
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))
        return round(price * exchange_rate, 2)

class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = ['country', 'region', 'town', 'address', 'mobile', 'email']

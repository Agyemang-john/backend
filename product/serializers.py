from rest_framework import serializers
from product.models import  *
from order.models import *
from core.models import *
from django.contrib.auth import get_user_model
from address.models import Country
from core.service import get_exchange_rates
from decimal import Decimal

User = get_user_model()



class UserSerializer(serializers.ModelSerializer):
    profile = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'email', 'phone', 'role', 'profile']

    def get_profile(self, obj):
        request = self.context.get("request")
        if hasattr(obj, "profile") and obj.profile.profile_image:
            if request is not None:
                return request.build_absolute_uri(obj.profile.profile_image.url)
            return obj.profile.profile_image.url
        return None

class MainCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Main_Category
        fields = '__all__'

class CategorySerializer(serializers.ModelSerializer):
    main_category = MainCategorySerializer()

    class Meta:
        model = Category
        fields = '__all__'

class SubCategorySerializer(serializers.ModelSerializer):
    category = CategorySerializer()

    class Meta:
        model = Sub_Category
        fields = '__all__'

class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = '__all__'

class OpeningHourSerializer(serializers.ModelSerializer):
    day = serializers.CharField(source='get_day_display')  # Display day name instead of integer

    class Meta:
        model = OpeningHour
        fields = ['day', 'from_hour', 'to_hour', 'is_closed']

class AboutSerializer(serializers.ModelSerializer):
    # If you want to display related fields (like vendor's email or name), add custom fields
    vendor_email = serializers.EmailField(source="vendor.email", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    
    class Meta:
        model = About
        fields = [
            'vendor_email', 'vendor_name', 'profile_image', 'cover_image', 'address', 
            'about', 'latitude', 'longitude', 'shipping_on_time', 'chat_resp_time', 
            'authentic_rating', 'day_return', 'waranty_period', 'facebook_url', 
            'instagram_url', 'twitter_url', 'linkedin_url'
        ]

class VendorSerializer(serializers.ModelSerializer):
    opening_hours = OpeningHourSerializer(many=True, read_only=True, source='openinghour_set')
    is_open_now = serializers.SerializerMethodField()  # Custom field to check if the vendor is open now
    about = AboutSerializer(read_only=True)

    class Meta:
        model = Vendor
        fields = [
            'id', 'name', 'slug', 'about', 'email', 'country', 'contact', 'is_featured', 'is_approved', 'followers', 
            'is_subscribed', 'subscription_end_date', 'created_at', 'modified_at', 'is_open_now', 'opening_hours'
        ]

    def get_is_open_now(self, obj):
        return obj.is_open()

class CountrySerializer(serializers.ModelSerializer):
    
    class Meta:
        model = Country
        fields = '__all__'

class ProductReviewSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)  # Use StringRelatedField to display the user, but make it read-only
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())  # Handle product as a related field
    product_image = serializers.SerializerMethodField()
    

    class Meta:
        model = ProductReview
        fields = ['review', 'rating', 'product', 'user', 'date', 'product_image']  # Include 'user' as read-only
        extra_kwargs = {'user': {'read_only': True}}
    
    def get_product_image(self, obj):
        # Access the image field from the related Product instance
        return obj.product.image.url if obj.product.image else None

    def create(self, validated_data):
        user = self.context['request'].user
        product = validated_data['product']

        # Prevent multiple reviews
        if ProductReview.objects.filter(user=user, product=product).exists():
            raise serializers.ValidationError("You have already reviewed this product.")

        return ProductReview.objects.create(user=user, **validated_data)

class ProductSerializer(serializers.ModelSerializer):
    sub_category = SubCategorySerializer()
    vendor = VendorSerializer()
    brand = BrandSerializer()
    available_in_regions = CountrySerializer(many=True)
    reviews = ProductReviewSerializer(many=True, read_only=True)
    currency = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "slug",
            "sub_category",
            "vendor",
            "reviews",
            "variant",
            "brand",
            "status",
            "title",
            "image",
            "video",
            "price",             # Now handled by get_price
            "old_price",         # Now handled by get_old_price
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
            "deals_of_the_day",
            "recommended_for_you",
            "popular_product",
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

    def get_old_price(self, obj):
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        if currency:
            rates = get_exchange_rates()
            exchange_rate = Decimal(str(rates.get(currency, 1)))
            return round(obj.old_price * exchange_rate, 2)
        return obj.old_price
    
    def get_price(self, obj):
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        rates = get_exchange_rates()  # Make sure this is imported and working

        exchange_rate = Decimal(str(rates.get(currency, 1)))# Default to 1 if currency not found
        return round(obj.price * exchange_rate, 2)
   

class ProductImageSerializer(serializers.ModelSerializer):
    # product = ProductSerializer()

    class Meta:
        model = ProductImages
        fields = '__all__'

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
    price = serializers.SerializerMethodField()  # <-- Add this line

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


class VariantImageSerializer(serializers.ModelSerializer):
    variant = VariantSerializer()

    class Meta:
        model = VariantImage
        fields = '__all__'


class WishlistSerializer(serializers.ModelSerializer):
    user = serializers.HiddenField(default=serializers.CurrentUserDefault())
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())

    class Meta:
        model = Wishlist
        fields = ['id', 'user', 'product', 'saved_at']

    def validate_product(self, value):
        """
        Ensure the product exists and is active (optional business rule).
        """
        if not value.status == 'published':
            raise serializers.ValidationError("The product is not available for saving.")
        return value

    def create(self, validated_data):
        """
        Ensure that the same product cannot be added multiple times for the same user.
        """
        user = validated_data['user']
        product = validated_data['product']
        wishlist_item, created = Wishlist.objects.get_or_create(user=user, product=product)
        if not created:
            raise serializers.ValidationError("This product is already in your wishlist.")
        return wishlist_item

class ProductDeliveryOptionSerializer(serializers.ModelSerializer):
    delivery_date_range = serializers.SerializerMethodField()

    class Meta:
        model = ProductDeliveryOption
        fields = ['product', 'variant', 'delivery_option', 'default', 'delivery_date_range']

    # This method calls the get_delivery_date_range method from the model
    def get_delivery_date_range(self, obj):
        return obj.get_delivery_date_range()

class CouponSerializer(serializers.ModelSerializer):
    is_valid = serializers.SerializerMethodField()

    class Meta:
        model = Coupon
        fields = ['code', 'discount_amount', 'discount_percentage', 'valid_from', 'valid_to', 'active', 'max_uses', 'used_count', 'min_purchase_amount', 'is_valid']

    # Custom method to return the validity status of the coupon
    def get_is_valid(self, obj):
        return obj.is_valid()
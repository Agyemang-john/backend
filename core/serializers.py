"""
core/serializers.py
Serializers used by homepage and core API views:
- UserSerializer: lightweight user representation with profile image
- ProductSerializer: product card data with currency-converted prices
- SubCategorySerializer: subcategory card for navigation
- TopEngagedCategorySerializer: category with nested subcategories
- CategoryWithSubcategoriesSerializer: category detail with subcategories + products
- MainCategoryWithCategoriesAndSubSerializer: full menu tree
- BrandSerializer, VendorSerializer, AboutSerializer, OpeningHourSerializer
- ProductReviewSerializer: product review with user info
- HomeSliderSerializer, BannersSerializer: promotional content
"""

from rest_framework import serializers
from product.models import *
from order.models import *
from .models import *
from django.contrib.auth import get_user_model
from address.models import *
from .service import get_exchange_rates
from decimal import Decimal
import random

User = get_user_model()


class DealsProductSerializer(serializers.ModelSerializer):
    """Lightweight serializer for the deals cache — always stores raw GHS prices."""

    class Meta:
        model = Product
        fields = ['id', 'title', 'slug', 'image', 'price', 'old_price', 'sku', 'sub_category']

class TrendingProductSerializer(serializers.ModelSerializer):
    """Raw GHS prices for cache — no currency conversion."""
    class Meta:
        model = Product
        fields = ['id', 'title', 'slug', 'image', 'price', 'old_price', 'sku', 'sub_category']

class HomepageProductSerializer(serializers.ModelSerializer):
    """Raw GHS prices for homepage cache — no currency conversion."""
    average_rating = serializers.FloatField(read_only=True, default=0)
    review_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Product
        fields = ['id', 'title', 'slug', 'image', 'price', 'old_price', 'sku', 'sub_category', 'average_rating', 'review_count']


class UserSerializer(serializers.ModelSerializer):
    profile = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id','first_name', 'last_name', 'email', 'phone', 'role', 'profile']

    def get_profile(self, obj):
        request = self.context.get("request")
        if hasattr(obj, "profile") and obj.profile.profile_image:
            if request is not None:
                return request.build_absolute_uri(obj.profile.profile_image.url)
            return obj.profile.profile_image.url
        return None

class ProductSerializer(serializers.ModelSerializer):
    currency = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()


    class Meta:
        model = Product
        fields = ['id', 'title', 'slug', 'image', 'price', 'sku', 'old_price', "currency", "sub_category"]
    
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
        request = self.context.get('request')
        currency = request.headers.get('X-Currency', 'GHS') if request else 'GHS'
        if currency:
            rates = get_exchange_rates()
            exchange_rate = Decimal(str(rates.get(currency, 1)))
            return round(obj.old_price * exchange_rate, 2)
        return obj.old_price

class SubCategorySerializer(serializers.ModelSerializer):
    image = serializers.ImageField(use_url=True)

    class Meta:
        model = Sub_Category
        fields = ['id', 'title', 'slug', 'image']

class TopEngagedCategorySerializer(serializers.ModelSerializer):
    subcategories = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ['id', 'title', 'slug', 'engagement_score', 'subcategories']

    def get_subcategories(self, obj):
        subcategories = Sub_Category.objects.filter(category=obj)
        return SubCategorySerializer(subcategories, many=True, context=self.context).data


class CategoryWithSubcategoriesSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(use_url=True)
    subcategories = serializers.SerializerMethodField()
    products = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ['id', 'title', 'slug', 'image', 'subcategories', 'products']

    def get_subcategories(self, obj):
        subcategories = Sub_Category.objects.filter(category=obj)
        return SubCategorySerializer(subcategories, many=True, context=self.context).data
    
    def get_products(self, obj):
        # Get all published products under this category
        products = list(Product.objects.filter(sub_category__category=obj, status='published'))

        # Shuffle the list
        random.shuffle(products)

        # Limit to 15
        products = products[:15]

        return ProductSerializer(products, many=True, context=self.context).data


class MainCategoryWithCategoriesAndSubSerializer(serializers.ModelSerializer):
    categories = serializers.SerializerMethodField()

    class Meta:
        model = Main_Category
        fields = ['id', 'title', 'slug', 'categories']

    def get_categories(self, obj):
        categories = Category.objects.filter(main_category=obj)
        # Pass context so nested serializers can build absolute URLs
        return CategoryWithSubcategoriesSerializer(categories, many=True, context=self.context).data

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


class ProductReviewSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)  # Use StringRelatedField to display the user, but make it read-only
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())  # Handle product as a related field
    product_image = serializers.SerializerMethodField()
    

    class Meta:
        model = ProductReview
        fields = ['review', 'rating', 'product', 'user', 'date', 'product_image']  # Include 'user' as read-only
        extra_kwargs = {'user': {'read_only': True}}
    
    def get_product_image(self, obj):
        request = self.context.get("request")
        if obj.product and obj.product.image:  # Check if product exists and has an image
            if request is not None:
                return request.build_absolute_uri(obj.product.image.url)  # Return full URL with domain
            return obj.product.image.url  # Return relative URL if no request context
        return None

    def create(self, validated_data):
        """Assign the authenticated user as the review author on creation."""
        user = self.context['request'].user
        review = ProductReview.objects.create(user=user, **validated_data)
        return review


class HomeSliderSerializer(serializers.ModelSerializer):
    class Meta:
        model = HomeSlider
        fields = [
            'id', 'title', 'subtitle', 'description', 'deal_type',
            'price_prefix', 'price', 'image_desktop', 'image_mobile',
            'link_url', 'cta_label', 'text_theme', 'content_align',
            'cta_position', 'is_active', 'order',
        ]

class BannersSerializer(serializers.ModelSerializer):
    class Meta:
        model = Banners
        fields = '__all__'


class PromoCardSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = PromoCard
        fields = [
            'id', 'title', 'eyebrow', 'link_url', 'link_text',
            'image', 'card_color', 'badge_text', 'badge_color',
            'text_color', 'link_color', 'is_tall', 'position',
        ]

    def get_image(self, obj):
        request = self.context.get('request')
        if obj.image:
            try:
                url = obj.image.url
                return request.build_absolute_uri(url) if request else url
            except Exception:
                return None
        return None


from rest_framework import serializers
from product.models import  *
from order.models import *
from django.contrib.auth import get_user_model
from django.contrib.auth import authenticate
from rest_framework.response import Response
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from userauths.utils import send_sms
from django.utils import timezone
from datetime import timedelta
from userauths.tokens import otp_token_generator
from django.db.models.query_utils import Q
from address.models import *
from django.utils.text import slugify


User = get_user_model()

from .models import Vendor, OpeningHour, Message, About

# Serializer for the OpeningHour model
class OpeningHourSerializer(serializers.ModelSerializer):
    day_display = serializers.CharField(source='get_day_display', read_only=True)

    class Meta:
        model = OpeningHour
        fields = ['id', 'vendor', 'day', 'day_display', 'from_hour', 'to_hour', 'is_closed']
        read_only_fields = ['vendor', 'id']
    
    def validate(self, data):
        # Ensure that there are no duplicate days for the same vendor
        user = self.context['request'].user
        vendor = getattr(user, 'vendor_user', None)  # Use `vendor_user` per related_name

        if vendor and OpeningHour.objects.filter(vendor=vendor, day=data['day']).exists():
            raise serializers.ValidationError("An opening DAY entry for this day already exists.")
        
        return data


# Serializer for the Vendor model
class VendorSerializer(serializers.ModelSerializer):
    country_name = serializers.CharField(source='country.name', read_only=True)
    followers_count = serializers.IntegerField(source='followers.count', read_only=True)
    is_open = serializers.SerializerMethodField()
    has_active_subscription = serializers.SerializerMethodField()
    subscription_due_soon = serializers.SerializerMethodField()

    def get_is_open(self, obj):
        return obj.is_open()

    def get_has_active_subscription(self, obj):
        return obj.has_active_subscription()

    def get_subscription_due_soon(self, obj):
        return obj.subscription_due_soon()

    class Meta:
        model = Vendor
        fields = [
            'id', 'name', 'slug', 'user', 'email', 'country', 'country_name', 'followers_count',
            'license', 'is_featured', 'is_approved', 'is_subscribed', 'subscription_end_date',
            'created_at', 'modified_at', 'is_open', 'has_active_subscription', 'subscription_due_soon',
            'student_id', 'contact', 'business_type', 'vendor_type',
        ]
    
    def validate(self, data):
        if data['vendor_type'] == 'student' and not data.get('student_id'):
            raise serializers.ValidationError("Students must upload a valid student ID.")
        if data['vendor_type'] == 'non_student' and not data.get('license'):
            raise serializers.ValidationError("Non-students must upload a valid business license.")
        return data

# Serializer for the About model
class AboutSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source='vendor.name', required=False)
    profile_image_url = serializers.ImageField(read_only=True)
    cover_image_url = serializers.ImageField(read_only=True)
    vendor = VendorSerializer()

    class Meta:
        model = About
        fields = [
            'id', 'vendor', 'vendor_name', 'profile_image', 'profile_image_url', 'cover_image', 'cover_image_url',
            'address', 'about', 'latitude', 'longitude', 'shipping_on_time', 'chat_resp_time', 'authentic_rating',
            'day_return', 'waranty_period', 'facebook_url', 'instagram_url', 'twitter_url', 'linkedin_url'
        ]
    
    def update(self, instance, validated_data):
        # Extract vendor data if provided
        vendor_data = validated_data.pop('vendor', {})
        if 'name' in vendor_data:
            # Update the vendor's name
            instance.vendor.name = vendor_data['name']
            instance.vendor.save()

        # Update remaining About fields
        return super().update(instance, validated_data)

# Serializer for the Message model
class MessageSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = Message
        fields = ['id', 'body', 'sent_by', 'created_at', 'created_by', 'created_by_username']

class SubCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Sub_Category
        fields = '__all__'

class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = '__all__'

class RegionSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = Region
        fields = '__all__'
        
class ProductImagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImages
        fields = ['id', 'images']

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

    def create(self, validated_data):
        # Pop user from context and assign it explicitly
        user = self.context['request'].user
        review = ProductReview.objects.create(user=user, **validated_data)
        return review


class ProductSerializer(serializers.ModelSerializer):
    # Use PrimaryKeyRelatedField to accept ID for foreign keys
    brand = serializers.PrimaryKeyRelatedField(queryset=Brand.objects.all())
    sub_category = serializers.PrimaryKeyRelatedField(queryset=Sub_Category.objects.all())
    available_in_regions = serializers.PrimaryKeyRelatedField(queryset=Region.objects.all(), many=True)

    class Meta:
        model = Product
        fields = '__all__'  # Include or specify fields as needed
        read_only_fields = ['sku', 'views', 'date', 'updated', 'vendor']
    
    def validate_price(self, value):
        if value <= 0:
            raise serializers.ValidationError("Price must be a positive value.")
        return value

    def validate_available_in_regions(self, value):
        # Example custom validation for available_in_regions
        if len(value) < 1:
            raise serializers.ValidationError("At least one region must be selected.")
        return value

class DeliveryOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryOption
        fields = '__all__'  # This will include all fields in the model
        read_only_fields = []  # Specify any fields that should be read-only, if necessary

    def create(self, validated_data):
        """Override the create method if you want to customize the creation of DeliveryOption."""
        return DeliveryOption.objects.create(**validated_data)

    def update(self, instance, validated_data):
        """Override the update method if you want to customize the update of DeliveryOption."""
        instance.name = validated_data.get('name', instance.name)
        instance.description = validated_data.get('description', instance.description)
        instance.min_days = validated_data.get('min_days', instance.min_days)
        instance.max_days = validated_data.get('max_days', instance.max_days)
        instance.cost = validated_data.get('cost', instance.cost)
        instance.save()
        return instance


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

class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = ['country', 'region', 'town', 'address', 'mobile', 'email']

class VariantsSerializer(serializers.ModelSerializer):
    combined_title = serializers.SerializerMethodField()

    class Meta:
        model = Variants
        fields = [
            'id', 'title', 'product', 'size', 'color', 
            'image', 'quantity', 'price', 'sku', "combined_title"
        ]
        read_only_fields = ['sku', 'id']
    
    def get_combined_title(self, obj):
        """
        Fetch the combined title from the Variants model method.
        """
        return obj.get_combined_title()
    
class OrderProductSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    variant = VariantsSerializer()
    delivery_date_range = serializers.SerializerMethodField()
    selected_delivery_option = DeliveryOptionSerializer()

    class Meta:
        model = OrderProduct
        fields = ['product','id', 'variant', 'quantity', 'price', 'amount', 'status', 'delivery_date_range', 'selected_delivery_option']
    
    def get_delivery_date_range(self, obj):
        """
        Dynamically calculate delivery status based on the order's creation date
        and the selected delivery option's delivery range, displaying it nicely.
        """
        from datetime import timedelta, datetime

        delivery_option = obj.selected_delivery_option
        if not delivery_option:
            return "Delivery option not selected"

        order_date = obj.order.date_created
        now = datetime.now()

        # Calculate delivery date range
        delivery_start_date = order_date + timedelta(days=delivery_option.min_days)
        delivery_end_date = order_date + timedelta(days=delivery_option.max_days)

        # Determine the delivery range text
        if now.date() > delivery_end_date.date():
            return "OVER"
        elif now.date() < delivery_start_date.date():
            days_until_start = (delivery_start_date.date() - now.date()).days
            start_label = "TOMORROW" if days_until_start == 1 else f"{delivery_start_date.date().strftime('%d %B')}"
            return f"{start_label} to {delivery_end_date.strftime('%d %B')}"
        elif delivery_start_date.date() <= now.date() <= delivery_end_date.date():
            if now.date() == delivery_start_date.date():
                return f"from TODAY to {delivery_end_date.strftime('%d %B')}"
            return f"ONGOING to {delivery_end_date.strftime('%d %B')}"

        return "Delivery status unavailable"




class OrderSerializer(serializers.ModelSerializer):
    order_products = serializers.SerializerMethodField()  # Nested serializer for order products
    address = AddressSerializer()
    vendor_delivery_date_range = serializers.SerializerMethodField()
    vendor_total = serializers.SerializerMethodField()
    vendor_delivery_cost = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'address', 'status', 'total', 
            'payment_method', 'date_created', 'order_products', 'vendor_delivery_date_range', 'vendor_total', 'vendor_delivery_cost',
        ]
    
    def get_vendor_delivery_date_range(self, obj):
        """Retrieve the delivery date range for the vendor's products in the order."""
        vendor = self.context['vendor']  # Get vendor from the context
        return obj.get_vendor_delivery_date_range(vendor)

    def get_order_products(self, obj):
        """Filter the order products by vendor and return them."""
        vendor = self.context['vendor']  # Get vendor from the context
        
        # Filter the products related to the vendor for the current order
        order_products = OrderProduct.objects.filter(order=obj, product__vendor=vendor)

        # Serialize the filtered order products
        return OrderProductSerializer(order_products, many=True).data
    
    def get_vendor_total(self, obj):
        """Calculate the total amount for the vendor's products in the order."""
        vendor = self.context['vendor']  # Get vendor from the context
        return obj.get_vendor_total(vendor)
    
    def get_vendor_delivery_cost(self, obj):
        """Calculate the total delivery cost for the vendor's products in the order."""
        vendor = self.context['vendor']  # Get vendor from the context
        return obj.get_vendor_delivery_cost(vendor)

    
    



class VendorRegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = '__all__'
        read_only_fields = ['user']
        extra_kwargs = {
            'slug': {'required': False}  # Make the slug field optional
        }

    def validate(self, data):
        # Validate that either license or student ID is provided based on vendor type
        if data['vendor_type'] == 'student' and not data.get('student_id'):
            raise serializers.ValidationError("Students must upload a valid student ID.")
        if data['vendor_type'] == 'non_student' and not data.get('license'):
            raise serializers.ValidationError("Non-students must upload a valid business license.")
        return data

    def create(self, validated_data):
        # Generate a slug from the name
        name = validated_data.get('name')
        validated_data['slug'] = slugify(name)

        # Ensure the slug is unique
        original_slug = validated_data['slug']
        counter = 1
        while Vendor.objects.filter(slug=validated_data['slug']).exists():
            validated_data['slug'] = f"{original_slug}-{counter}"
            counter += 1

        # Create the Vendor instance
        return Vendor.objects.create(**validated_data)

    
class VendorPaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorPaymentMethod
        fields = [
            'payment_method',
            'momo_number',
            'momo_provider',
            'bank_name',
            'bank_account_name',
            'bank_account_number',
            'bank_routing_number',
            'country',
            'vendor',
        ]

    def validate(self, data):
        payment_method = data.get('payment_method')

        if payment_method == 'momo':
            if not data.get('momo_number') or not data.get('momo_provider'):
                raise serializers.ValidationError("Mobile Money details must be provided.")
        elif payment_method == 'bank':
            if not (data.get('bank_name') and data.get('bank_account_name') and data.get('bank_account_number')):
                raise serializers.ValidationError("Bank details must be provided.")
        
        return data


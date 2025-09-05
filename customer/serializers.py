from rest_framework import serializers
from product.models import  *
from order.models import *
from django.contrib.auth import get_user_model
from django.contrib.auth import authenticate
from rest_framework.response import Response
from userauths.utils import send_sms
from django.utils import timezone
from datetime import timedelta
from userauths.tokens import otp_token_generator
from django.db.models.query_utils import Q
from address.models import *
from userauths.models import Profile
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate


User = get_user_model()



class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'email', 'phone']


class ProfileSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(source='user.first_name', required=False)
    last_name = serializers.CharField(source='user.last_name', required=False)
    email = serializers.CharField(source='user.email', required=False)
    user = UserSerializer()
    profile_image = serializers.ImageField(required=False)

    class Meta:
        model = Profile
        fields = [
            'first_name',
            'last_name',
            'email',
            'user',
            'profile_image',
            'mobile',
            'country',
            'date_of_birth',
            'gender',
            'address',
            'newsletter_subscription',
            'latitude',
            'longitude',
        ]

    def update(self, instance, validated_data):
        # Extract user-related data
        user_data = validated_data.pop('user', {})

        # Update User model fields
        user = instance.user
        for field, value in user_data.items():
            if value is not None:
                setattr(user, field, value)
        user.save()

        # Update Profile fields
        for field, value in validated_data.items():
            if field == 'profile_image' and value is None:
                continue  # Skip updating profile_image if no file is provided
            setattr(instance, field, value)
        instance.save()

        return instance


class ProductSerializer(serializers.ModelSerializer):

    class Meta:
        model = Product
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

    class Meta:
        model = Variants
        fields = '__all__'

class VariantImageSerializer(serializers.ModelSerializer):
    variant = VariantSerializer()

    class Meta:
        model = VariantImage
        fields = '__all__'

class ProductDeliveryOptionSerializer(serializers.ModelSerializer):
    delivery_date_range = serializers.SerializerMethodField()

    class Meta:
        model = ProductDeliveryOption
        fields = ['product', 'variant', 'delivery_option', 'default', 'delivery_date_range']

    # This method calls the get_delivery_date_range method from the model
    def get_delivery_date_range(self, obj):
        return obj.get_delivery_date_range()

class SavedProductSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects)



class SubCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Sub_Category
        fields = '__all__'


#################################AUTH#########################################

class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = ['id', 'user','latitude','longitude','full_name', 'country', 'region', 'town', 'address', 'gps_address', 'email', 'mobile', 'status','date_added']
        read_only_fields = ['id', 'date_added']

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


class OrderProductSerializer(serializers.ModelSerializer):
    delivery_range = serializers.SerializerMethodField()
    variant = VariantSerializer()
    product = ProductSerializer()
    selected_delivery_option = DeliveryOptionSerializer()
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
            'selected_delivery_option',
            'date_created',
            'date_updated',
            'delivery_range',
        ]
    def get_delivery_range(self, obj):
        return obj.get_delivery_range()

class OrderSerializer(serializers.ModelSerializer):
    order_products = OrderProductSerializer(many=True, read_only=True)
    overall_delivery_message = serializers.SerializerMethodField()
    address = AddressSerializer()

    class Meta:
        model = Order
        fields = [
            'id',
            'user',
            'order_number',
            'payment_id',
            'address',
            'payment_method',
            'total',
            'status',
            'ip',
            'adminnote',
            'is_ordered',
            'date_created',
            'date_updated',
            'order_products',  # Nested OrderProduct data
            'overall_delivery_message',
        ]
    def get_overall_delivery_message(self, obj):
        return obj.get_overall_delivery_range()




class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
    confirm_new_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        request = self.context.get('request')
        user = request.user

        # Check if the old password is correct
        if not user.check_password(attrs['old_password']):
            raise serializers.ValidationError({"old_password": "Old password is not correct."})

        # Check if new password matches confirmation
        if attrs['new_password'] == attrs['old_password']:
            raise serializers.ValidationError({"new_password": "New password can't be same as old."})
        
        if attrs['new_password'] != attrs['confirm_new_password']:
            raise serializers.ValidationError({"new_password": "New password fields didn't match."})

        return attrs

    def save(self, **kwargs):
        request = self.context.get('request')
        user = request.user
        user.set_password(self.validated_data['new_password'])
        user.save()
        return user

class ProductReviewSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.title", read_only=True)
    
    class Meta:
        model = ProductReview
        fields = ['id', 'product', 'product_name', 'review', 'rating', 'status', 'date']
        read_only_fields = ['product_name', 'status', 'date']


class WishlistSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    product = ProductSerializer()

    class Meta:
        model = Wishlist
        fields = '__all__'

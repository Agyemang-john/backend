# notifications/serializers.py
from rest_framework import serializers
from .models import Notification
from django.contrib.humanize.templatetags.humanize import naturaltime
from order.models import Order, OrderProduct

class NotificationSerializer(serializers.ModelSerializer):
    verb_display = serializers.CharField(source="get_verb_display", read_only=True)
    time_ago = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "verb", "verb_display", "data", "is_read", "created_at", "time_ago", "url"]

    def get_time_ago(self, obj):
        return naturaltime(obj.created_at)

    def get_url(self, obj):
        return obj.data.get("url", "#")


# contact/serializers.py
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import ContactInquiry, Report, SupportTicket
from order.models import Order  # Adjust import path as needed
import uuid

User = get_user_model()


class ContactInquirySerializer(serializers.ModelSerializer):
    order = serializers.CharField(required=False, allow_null=True, allow_blank=True) 

    class Meta:
        model = ContactInquiry
        fields = ['name', 'email', 'phone', 'inquiry_type', 'subject', 'message', 'order']
        extra_kwargs = {
            'phone': {'required': False, 'allow_blank': True},
        }

    def validate_message(self, value):
        if len(value.strip()) < 20:
            raise serializers.ValidationError("Message must be at least 20 characters long.")
        return value.strip()

    def validate_order(self, value):
        if not value:
            return None
        try:
            # Accept string UUID → convert safely
            return Order.objects.get(id=str(value).strip())
        except (Order.DoesNotExist, ValueError):
            raise serializers.ValidationError("Invalid or unauthorized order ID.")

    def validate(self, data):
        request = self.context['request']
        order = data.get('order')

        # If order selected, must belong to logged-in user
        if order and request.user.is_authenticated:
            if order.user != request.user:
                raise serializers.ValidationError({"order": "You can only reference your own orders."})

        # Auto-fill name/email for guests who are actually logged in
        if request.user.is_authenticated:
            if not data.get('name'):
                data['name'] = request.user.get_full_name() or request.user.email.split('@')[0]
            if not data.get('email'):
                data['email'] = request.user.email

        return data

    def create(self, validated_data):
        request = self.context['request']
        user = request.user if request.user.is_authenticated else None

        inquiry = ContactInquiry.objects.create(user=user, **validated_data)
        SupportTicket.objects.create(inquiry=inquiry)  # auto ticket
        return inquiry


class ReportSerializer(serializers.ModelSerializer):
    target_id = serializers.IntegerField(write_only=True)
    target_type = serializers.CharField(write_only=True)  # e.g., "product", "review"

    class Meta:
        model = Report
        fields = ['reason', 'description', 'target_type', 'target_id', 'attachments']
        read_only_fields = ['reporter', 'status']

    def validate_description(self, value):
        if len(value.strip()) < 30:
            raise serializers.ValidationError("Description must be at least 30 characters.")
        return value.strip()

    def validate(self, data):
        from django.contrib.contenttypes.models import ContentType

        target_type = data.get('target_type')
        target_id = data.get('target_id')

        if not target_type or not target_id:
            raise serializers.ValidationError("Both target_type and target_id are required.")

        try:
            content_type = ContentType.objects.get(model=target_type.lower())
            target = content_type.get_object_for_this_type(id=target_id)
        except (ContentType.DoesNotExist, content_type.model_class().DoesNotExist):
            raise serializers.ValidationError("Invalid target object.")

        data['content_type'] = content_type
        data['object_id'] = target_id
        return data

    def create(self, validated_data):
        validated_data.pop('target_type', None)
        validated_data.pop('target_id', None)
        validated_data['reporter'] = self.context['request'].user
        return super().create(validated_data)


class OrderProductSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    
    class Meta:
        model = OrderProduct
        fields = ['product_name', 'quantity', 'amount']

class OrderSerializer(serializers.ModelSerializer):
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    order_number = serializers.CharField(read_only=True)
    date_created = serializers.DateTimeField(format="%b %d, %Y", read_only=True)
    items = OrderProductSerializer(source='order_products', many=True, read_only=True)
    
    class Meta:
        model = Order
        fields = [
            'id',
            'order_number',
            'total_price',
            'total',  # fallback if total_price not used
            'status',
            'date_created',
            'items'
        ]
"""
vendor/operations_serializers.py
Shapes for the seller operations endpoints (vendor/operations_views.py):
store operations settings, ledger, returns and review replies.
"""

from rest_framework import serializers

from order.models import ReturnRequest
from payments.ledger_models import LedgerEntry
from payments.models import Payout
from product.models import ProductReview, ReviewReport

from .models import Vendor


class OperationsSettingsSerializer(serializers.ModelSerializer):
    handling_days = serializers.IntegerField(min_value=1, max_value=14)
    low_stock_threshold = serializers.IntegerField(min_value=0, max_value=10000)

    class Meta:
        model = Vendor
        fields = ['handling_days', 'low_stock_threshold']


class LedgerEntrySerializer(serializers.ModelSerializer):
    type_label = serializers.CharField(source='get_entry_type_display', read_only=True)
    order_number = serializers.CharField(source='order.order_number', read_only=True, default=None)
    status = serializers.SerializerMethodField()

    class Meta:
        model = LedgerEntry
        fields = ['id', 'entry_type', 'type_label', 'amount', 'currency', 'description', 'order_number',
                  'rate', 'available_at', 'status', 'payout', 'created_at']

    def get_status(self, obj):
        # paid: settled by a payout; available: can be paid; pending: on hold.
        if obj.payout_id:
            return 'paid'
        now = self.context.get('now')
        return 'available' if now and obj.available_at <= now else 'pending'


class PayoutSummarySerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Payout
        fields = ['id', 'amount', 'status', 'status_display', 'transaction_id', 'created_at']


class VendorReturnSerializer(serializers.ModelSerializer):
    reason_label = serializers.CharField(source='get_reason_display', read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    order_number = serializers.CharField(source='order.order_number', read_only=True)
    product_title = serializers.CharField(source='order_product.product.title', read_only=True, default='')
    product_image = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    line_quantity = serializers.IntegerField(source='order_product.quantity', read_only=True)

    class Meta:
        model = ReturnRequest
        fields = ['reference', 'order_number', 'product_title', 'product_image', 'customer_name',
                  'reason', 'reason_label', 'details', 'quantity', 'line_quantity', 'refund_amount',
                  'status', 'status_label', 'seller_note', 'created_at', 'decided_at', 'received_at',
                  'refunded_at']

    def get_product_image(self, obj):
        product = obj.order_product.product
        if not product or not product.image:
            return None
        request = self.context.get('request')
        url = product.image.url
        return request.build_absolute_uri(url) if request else url

    def get_customer_name(self, obj):
        # First name + initial only; sellers don't need the full identity.
        user = obj.customer
        if not user:
            return 'Customer'
        return f"{user.first_name} {user.last_name[:1]}.".strip()


class ReturnDecisionSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default='')


class ReviewReplySerializer(serializers.Serializer):
    reply = serializers.CharField(min_length=2, max_length=1000, trim_whitespace=True)


class ReviewReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewReport
        fields = ['reason', 'details']


class SellerReviewSerializer(serializers.ModelSerializer):
    """Seller's view of a review: read-only, plus their own reply."""
    product_title = serializers.CharField(source='product.title', read_only=True, default='')
    customer_name = serializers.SerializerMethodField()
    has_open_report = serializers.SerializerMethodField()
    media = serializers.SerializerMethodField()

    class Meta:
        model = ProductReview
        fields = ['id', 'product', 'product_title', 'customer_name', 'rating', 'review', 'status',
                  'is_verified_purchase', 'purchased_variant', 'helpful_count', 'media',
                  'seller_reply', 'seller_replied_at', 'has_open_report', 'date']

    def get_media(self, obj):
        from product.review_serializers import ReviewMediaSerializer
        visible = [m for m in obj.media.all() if not m.is_hidden]
        return ReviewMediaSerializer(visible, many=True, context=self.context).data

    def get_customer_name(self, obj):
        user = obj.user
        return f"{user.first_name} {user.last_name[:1]}.".strip() if user else 'Customer'

    def get_has_open_report(self, obj):
        return any(r.status == 'open' for r in obj.reports.all())

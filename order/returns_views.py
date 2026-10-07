"""
order/returns_views.py
Customer-facing returns API and the delivery-provider webhook.

    GET   /api/v1/order/returns/?order=<id>        my return requests (optionally for one order)
    POST  /api/v1/order/returns/                   {order_product, reason, details?, quantity?}
    POST  /api/v1/order/returns/<ref>/cancel/
    GET   /api/v1/order/returns/reasons/           reason choices for the form
    POST  /api/v1/order/delivery/webhooks/<code>/  courier status updates (external providers)

Rules are in order/returns.py; this module only translates HTTP.
"""

import logging

from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import returns as return_rules
from .delivery import DeliveryProviderError, get_provider
from .fulfilment import record_tracking_event
from .models import OrderProduct, ReturnRequest, Shipment

logger = logging.getLogger(__name__)


class CustomerReturnSerializer(serializers.ModelSerializer):
    reason_label = serializers.CharField(source='get_reason_display', read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    order_product = serializers.IntegerField(source='order_product_id', read_only=True)
    product_title = serializers.CharField(source='order_product.product.title', read_only=True, default='')
    store_name = serializers.CharField(source='vendor.name', read_only=True, default='')

    class Meta:
        model = ReturnRequest
        fields = ['reference', 'order', 'order_product', 'product_title', 'store_name', 'reason',
                  'reason_label', 'details', 'quantity', 'refund_amount', 'status', 'status_label',
                  'seller_note', 'created_at', 'decided_at', 'received_at', 'refunded_at']


class OpenReturnSerializer(serializers.Serializer):
    order_product = serializers.IntegerField()
    reason = serializers.ChoiceField(choices=ReturnRequest.REASON_CHOICES)
    details = serializers.CharField(max_length=2000, required=False, allow_blank=True, default='')
    quantity = serializers.IntegerField(min_value=1, required=False, default=1)


class CustomerReturnListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            ReturnRequest.objects.filter(customer=request.user)
            .select_related('order_product__product', 'vendor')
        )
        order_id = request.query_params.get('order')
        if order_id and order_id.isdigit():
            qs = qs.filter(order_id=int(order_id))
        return Response(CustomerReturnSerializer(qs[:200], many=True).data)

    def post(self, request):
        payload = OpenReturnSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        line = get_object_or_404(
            OrderProduct.objects.select_related('order', 'product__vendor'),
            pk=data['order_product'], order__user=request.user,
        )
        try:
            rr = return_rules.open_return(
                customer=request.user, line=line, reason=data['reason'],
                details=data['details'], quantity=data['quantity'],
            )
        except return_rules.ReturnError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CustomerReturnSerializer(rr).data, status=status.HTTP_201_CREATED)


class CustomerReturnCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, reference):
        rr = get_object_or_404(ReturnRequest, reference=reference, customer=request.user)
        try:
            return_rules.cancel_return(rr, request.user)
        except return_rules.ReturnError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CustomerReturnSerializer(rr).data)


class ReturnReasonsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response([
            {'value': value, 'label': label, 'seller_fault': value in ReturnRequest.SELLER_FAULT_REASONS}
            for value, label in ReturnRequest.REASON_CHOICES
        ])


class DeliveryWebhookView(APIView):
    """
    Entry point for external couriers. The provider class verifies the call
    (signature/secret) and translates it; unknown providers or providers
    without webhooks get 404. Negromart's own delivery doesn't use this.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, code):
        try:
            provider = get_provider(code)
            updates = provider.parse_webhook(request)
        except DeliveryProviderError as exc:
            logger.warning("delivery webhook rejected provider=%s: %s", code, exc)
            return Response({'detail': 'Not accepted.'}, status=status.HTTP_404_NOT_FOUND)

        applied = 0
        for update in updates:
            shipment = (
                Shipment.objects.select_related('order')
                .filter(provider=code)
                .filter(shipment_id=update.shipment_lookup).first()
                or Shipment.objects.select_related('order')
                .filter(provider=code, external_reference=update.shipment_lookup).first()
            )
            if shipment is None:
                logger.warning("delivery webhook: unknown shipment %s from %s", update.shipment_lookup, code)
                continue
            record_tracking_event(
                shipment, status=update.status, description=update.description,
                event_date=update.event_date, location=update.location, city=update.city,
                country=update.country,
            )
            applied += 1
        return Response({'applied': applied})

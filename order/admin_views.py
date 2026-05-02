"""
Admin-only REST API for delivery tracking management.

All views require request.user.is_staff == True.
Base path: /api/v1/order/admin/

Endpoints:
  GET  /admin/dashboard/            → live stats (orders by status, shipments in transit, etc.)
  GET  /admin/orders/               → paginated list of ALL orders with tracking summary
  GET  /admin/orders/<id>/          → full order detail + shipments + tracking events
  PUT  /admin/orders/<id>/status/   → force-update order status + broadcast WS
  POST /admin/orders/<id>/shipment/ → admin creates a shipment for any order
  PUT  /admin/orders/<id>/shipment/<sh_id>/         → admin updates a shipment
  POST /admin/orders/<id>/shipment/<sh_id>/event/   → admin adds a tracking event
  POST /admin/orders/<id>/broadcast/                → manually push WS snapshot to customer
"""

import logging
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Count, Q

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.pagination import PageNumberPagination
from rest_framework import status

from order.models import Order, Shipment, TrackingEvent, OrderProduct
from order.serializers import (
    OrderTrackingSerializer, ShipmentSerializer,
    TrackingEventSerializer, OrderSerializer,
)
from vendor.models import Vendor

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _broadcast(order_id):
    """Push the latest tracking snapshot to the customer WebSocket group."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        order_obj = Order.objects.prefetch_related(
            'shipments__tracking_events',
            'shipments__items__product',
            'shipments__vendor',
        ).get(id=order_id)
        data = OrderTrackingSerializer(order_obj).data
        async_to_sync(channel_layer.group_send)(
            f'order_tracking_{order_id}',
            {'type': 'tracking_update', 'data': data},
        )
    except Exception as e:
        logger.warning(f'Admin broadcast failed for order {order_id}: {e}')


class AdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard stats
# ─────────────────────────────────────────────────────────────────────────────

class AdminTrackingDashboardView(APIView):
    """
    GET /api/v1/order/admin/dashboard/
    Returns a live stats snapshot for the admin tracking dashboard.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        orders = Order.objects.filter(is_ordered=True)
        shipments = Shipment.objects.all()

        order_by_status = dict(
            orders.values_list('status')
                  .annotate(c=Count('id'))
                  .values_list('status', 'c')
        )

        shipment_by_status = dict(
            shipments.values_list('status')
                     .annotate(c=Count('id'))
                     .values_list('status', 'c')
        )

        # Orders that have no shipment yet but are not pending/canceled
        unshipped = orders.filter(
            status__in=('processing', 'shipped'),
        ).annotate(sh_count=Count('shipments')).filter(sh_count=0).count()

        # Shipments without any tracking events
        no_events = shipments.annotate(ev_count=Count('tracking_events')).filter(ev_count=0).count()

        # Recently delivered (last 24h)
        recent_deliveries = shipments.filter(
            status='delivered',
            delivered_at__gte=timezone.now() - timezone.timedelta(hours=24),
        ).count()

        return Response({
            'orders': {
                'total': orders.count(),
                'by_status': order_by_status,
            },
            'shipments': {
                'total': shipments.count(),
                'by_status': shipment_by_status,
                'no_events': no_events,
                'delivered_last_24h': recent_deliveries,
            },
            'alerts': {
                'unshipped_processing_orders': unshipped,
            },
        })


# ─────────────────────────────────────────────────────────────────────────────
# Order list
# ─────────────────────────────────────────────────────────────────────────────

class AdminOrderListView(APIView):
    """
    GET /api/v1/order/admin/orders/
    Query params: status, search, has_shipment (true/false), ordering (-date_created)
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        qs = Order.objects.filter(is_ordered=True).select_related(
            'user', 'address'
        ).prefetch_related(
            'shipments',
            'order_products',
        ).order_by('-date_created')

        # Filters
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)

        search = request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(order_number__icontains=search) |
                Q(user__email__icontains=search) |
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search)
            )

        has_shipment = request.query_params.get('has_shipment')
        if has_shipment == 'true':
            qs = qs.filter(shipments__isnull=False).distinct()
        elif has_shipment == 'false':
            qs = qs.filter(shipments__isnull=True)

        paginator = AdminPagination()
        page = paginator.paginate_queryset(qs, request)

        data = []
        for order in page:
            shipments = order.shipments.all()
            data.append({
                'id': order.id,
                'order_number': order.order_number,
                'status': order.status,
                'customer_email': order.user.email if order.user else None,
                'customer_name': f'{order.user.first_name} {order.user.last_name}'.strip() if order.user else None,
                'total': str(order.total),
                'payment_method': order.payment_method,
                'date_created': order.date_created,
                'item_count': order.order_products.count(),
                'shipment_count': shipments.count(),
                'shipment_statuses': [s.status for s in shipments],
                'address_summary': {
                    'town': order.address.town,
                    'region': order.address.region,
                    'country': order.address.country,
                } if order.address else None,
            })

        return paginator.get_paginated_response(data)


# ─────────────────────────────────────────────────────────────────────────────
# Order detail
# ─────────────────────────────────────────────────────────────────────────────

class AdminOrderDetailView(APIView):
    """
    GET /api/v1/order/admin/orders/<id>/
    Full order + shipments + tracking events.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, order_id):
        order = get_object_or_404(
            Order.objects.prefetch_related(
                'shipments__tracking_events',
                'shipments__items__product',
                'shipments__vendor',
                'order_products__product',
                'order_products__variant',
                'order_products__selected_delivery_option',
                'vendors',
            ).select_related('user', 'address'),
            id=order_id,
        )
        return Response({
            'order': OrderSerializer(order, context={'request': request}).data,
            'tracking': OrderTrackingSerializer(order).data,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Force order status
# ─────────────────────────────────────────────────────────────────────────────

class AdminOrderStatusView(APIView):
    """
    PUT /api/v1/order/admin/orders/<id>/status/
    Body: { "status": "shipped" }
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def put(self, request, order_id):
        order = get_object_or_404(Order, id=order_id)
        new_status = request.data.get('status')
        if new_status not in dict(Order.STATUS_CHOICES).keys():
            return Response({'error': 'Invalid status.'}, status=status.HTTP_400_BAD_REQUEST)

        order.status = new_status
        order.save(update_fields=['status'])
        _broadcast(order.id)

        return Response({
            'id': order.id,
            'order_number': order.order_number,
            'status': order.status,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Admin shipment management
# ─────────────────────────────────────────────────────────────────────────────

class AdminShipmentView(APIView):
    """
    GET  /api/v1/order/admin/orders/<id>/shipment/              → list
    POST /api/v1/order/admin/orders/<id>/shipment/              → create
    PUT  /api/v1/order/admin/orders/<id>/shipment/<sh_id>/      → update
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, order_id):
        order = get_object_or_404(Order, id=order_id)
        shipments = Shipment.objects.filter(order=order).prefetch_related('tracking_events', 'items__product')
        return Response(ShipmentSerializer(shipments, many=True).data)

    def post(self, request, order_id):
        order = get_object_or_404(Order, id=order_id)

        vendor_id = request.data.get('vendor_id')
        if not vendor_id:
            return Response({'error': 'vendor_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        vendor = get_object_or_404(Vendor, id=vendor_id)
        vendor_items = list(order.order_products.filter(product__vendor=vendor))
        if not vendor_items:
            return Response({'error': 'No items for this vendor in this order.'}, status=status.HTTP_400_BAD_REQUEST)

        shipment = Shipment.objects.create(
            order=order,
            vendor=vendor,
            carrier=request.data.get('carrier', ''),
            carrier_code=request.data.get('carrier_code', ''),
            tracking_number=request.data.get('tracking_number', ''),
            tracking_url=request.data.get('tracking_url', ''),
            status=request.data.get('status', 'label_created'),
            estimated_delivery_date=request.data.get('estimated_delivery_date') or None,
            is_international=request.data.get('is_international', False),
        )
        shipment.items.set(vendor_items)

        if order.status not in ('delivered', 'canceled'):
            order.status = 'shipped'
            order.save(update_fields=['status'])

        _broadcast(order.id)
        return Response(ShipmentSerializer(shipment).data, status=status.HTTP_201_CREATED)

    def put(self, request, order_id, shipment_id):
        order = get_object_or_404(Order, id=order_id)
        shipment = get_object_or_404(Shipment, shipment_id=shipment_id, order=order)

        for field in ('carrier', 'carrier_code', 'tracking_number', 'tracking_url',
                      'status', 'estimated_delivery_date', 'shipped_at', 'delivered_at', 'is_international'):
            val = request.data.get(field)
            if val is not None:
                setattr(shipment, field, val)
        shipment.save()

        _broadcast(order.id)
        return Response(ShipmentSerializer(shipment).data)


# ─────────────────────────────────────────────────────────────────────────────
# Admin tracking event
# ─────────────────────────────────────────────────────────────────────────────

class AdminTrackingEventView(APIView):
    """
    POST /api/v1/order/admin/orders/<id>/shipment/<sh_id>/event/
    Admin adds a tracking event to any shipment.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, order_id, shipment_id):
        order = get_object_or_404(Order, id=order_id)
        shipment = get_object_or_404(Shipment, shipment_id=shipment_id, order=order)

        for field in ('status', 'description', 'event_date'):
            if not request.data.get(field):
                return Response({'error': f"'{field}' is required."}, status=status.HTTP_400_BAD_REQUEST)

        if request.data['status'] not in dict(TrackingEvent.STATUS_CHOICES).keys():
            return Response({'error': 'Invalid event status.'}, status=status.HTTP_400_BAD_REQUEST)

        event = TrackingEvent.objects.create(
            shipment=shipment,
            status=request.data['status'],
            description=request.data['description'],
            location=request.data.get('location', ''),
            city=request.data.get('city', ''),
            country=request.data.get('country', ''),
            event_date=request.data['event_date'],
        )

        status_map = {
            'in_transit': 'in_transit',
            'out_for_delivery': 'out_for_delivery',
            'delivered': 'delivered',
            'failed_attempt': 'failed',
            'returned_to_sender': 'returned',
        }
        if event.status in status_map:
            shipment.status = status_map[event.status]
            shipment.save(update_fields=['status'])

        _broadcast(order.id)
        return Response(TrackingEventSerializer(event).data, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────────────────
# Manual broadcast
# ─────────────────────────────────────────────────────────────────────────────

class AdminBroadcastView(APIView):
    """
    POST /api/v1/order/admin/orders/<id>/broadcast/
    Manually push the current tracking snapshot to the customer's WebSocket.
    Useful to resync after manual DB edits.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request, order_id):
        order = get_object_or_404(Order, id=order_id)
        _broadcast(order.id)
        return Response({'detail': f'Tracking update broadcast for order {order.order_number}.'})

"""
WebSocket consumer for real-time order delivery tracking.

Customers connect to ws/order/<order_id>/tracking/?token=<jwt>
and receive live updates whenever a vendor updates the shipment status
or adds a new tracking event.
"""

import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model

from order.models import Order, Shipment
from order.serializers import OrderTrackingSerializer

User = get_user_model()
logger = logging.getLogger(__name__)


@database_sync_to_async
def get_order_for_user(order_id, user):
    try:
        return Order.objects.prefetch_related(
            'vendors',
            'shipments__tracking_events',
            'shipments__items__product',
            'shipments__vendor',
        ).get(id=order_id, user=user)
    except Order.DoesNotExist:
        return None


@database_sync_to_async
def serialize_order(order):
    return OrderTrackingSerializer(order).data


class OrderTrackingConsumer(AsyncWebsocketConsumer):
    """
    Real-time order tracking WebSocket.

    - Connects: validates JWT user owns the order, joins group `order_tracking_<id>`
    - On connect: sends current tracking snapshot
    - tracking_update event: pushed by vendor actions, relayed to customer
    """

    async def connect(self):
        self.order_id = self.scope['url_route']['kwargs']['order_id']
        self.user = self.scope.get('user')

        if not self.user or self.user.is_anonymous:
            await self.close(code=4001)
            return

        order = await get_order_for_user(self.order_id, self.user)
        if not order:
            await self.close(code=4004)
            return

        self.group_name = f"order_tracking_{self.order_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send full tracking snapshot on connect
        data = await serialize_order(order)
        await self.send(text_data=json.dumps({
            'type': 'tracking_snapshot',
            'data': data,
        }))
        logger.info(f"User {self.user.id} connected to order tracking for order {self.order_id}")

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    # ── Handlers for group_send events ───────────────────────────────────────

    async def tracking_update(self, event):
        """Relays a tracking update pushed by the vendor."""
        await self.send(text_data=json.dumps({
            'type': 'tracking_update',
            'data': event['data'],
        }))

    async def status_changed(self, event):
        """Relays an order status change (e.g. shipped → delivered)."""
        await self.send(text_data=json.dumps({
            'type': 'status_changed',
            'status': event['status'],
            'order_id': event['order_id'],
        }))

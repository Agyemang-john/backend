"""
chat/views.py
Shopper ↔ seller chat API. HTTP only; the rules are in chat/services.py.
Every endpoint takes ?as=customer (default, storefront) or ?as=vendor (seller dashboard).

    GET    /api/v1/chat/conversations/?as=&q=&unread=1&cursor=     chat list, newest activity first
    POST   /api/v1/chat/conversations/                             shopper starts/continues a chat
                                                                   {vendor, body, reason, product_id, client_id}
    GET    /api/v1/chat/conversations/<id>/?as=                    one chat
    GET    /api/v1/chat/conversations/<id>/messages/?as=&before=   history, 40 at a time, oldest first
    POST   /api/v1/chat/conversations/<id>/messages/?as=           {body, client_id, product_id}
    POST   /api/v1/chat/conversations/<id>/read/?as=               mark read (sends a read receipt)
    GET    /api/v1/chat/unread/?as=                                {conversations: n} for badges
    GET    /api/v1/chat/store/<slug>/                              the shopper's chat with a store, if any
    GET    /api/v1/chat/store/<slug>/presence/                     public: {online, last_seen} for the store page
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from product.models import Product
from vendor.access import get_membership
from vendor.models import Vendor

from . import realtime, services
from .models import Conversation, Message, Side
from .serializers import ConversationSerializer, MessageSerializer, serialize_conversations

HISTORY_PAGE = 40


class ChatSendThrottle(UserRateThrottle):
    scope = 'chat_send'
    rate = '40/min'


class ChatPagination(CursorPagination):
    page_size = 30
    ordering = ('-last_message_at', '-id')


def _side(request):
    return Side.VENDOR if request.query_params.get('as') == Side.VENDOR else Side.CUSTOMER


def _error(exc):
    return Response({'detail': str(exc), 'code': exc.code, **exc.extra}, status=exc.status)


def _conversation(request, pk, side):
    qs = services.conversations_for(request.user, side).select_related('customer', 'vendor__about', 'product')
    return get_object_or_404(qs, pk=pk)


def _product(product_id):
    if not product_id:
        return None
    try:
        return Product.objects.filter(pk=int(product_id)).first()
    except (TypeError, ValueError):
        return None


def _policy():
    return {
        'retention_days': services.retention_days(),
        'max_length': services.MAX_LENGTH,
    }


class ConversationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [ChatSendThrottle()] if self.request.method == 'POST' else super().get_throttles()

    def get(self, request):
        side = _side(request)
        qs = (services.conversations_for(request.user, side)
              .filter(last_message_at__isnull=False)
              .select_related('customer', 'vendor__about', 'product'))
        q = (request.query_params.get('q') or '').strip()[:60]
        if q:
            if side == Side.CUSTOMER:
                qs = qs.filter(vendor__name__icontains=q)
            else:
                qs = qs.filter(Q(customer__first_name__icontains=q) | Q(customer__last_name__icontains=q))
        if request.query_params.get('unread') in ('1', 'true'):
            qs = qs.filter(**{f'{side}_unread__gt': 0})
        paginator = ChatPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(serialize_conversations(page, side, request))
        response.data['policy'] = _policy()
        return response

    def post(self, request):
        vendor = get_object_or_404(Vendor, slug=str(request.data.get('vendor') or ''))
        try:
            conversation, message = services.start_conversation(
                request.user, vendor, request.data.get('body'),
                reason=request.data.get('reason') or Conversation.REASON_QUESTION,
                product=_product(request.data.get('product_id')),
                client_id=str(request.data.get('client_id') or ''),
            )
        except services.ChatError as exc:
            return _error(exc)
        conversation = _conversation(request, conversation.pk, Side.CUSTOMER)
        return Response({
            'conversation': ConversationSerializer(conversation, context={'side': Side.CUSTOMER, 'request': request}).data,
            'message': MessageSerializer(message, context={'request': request}).data,
        }, status=status.HTTP_201_CREATED)


class ConversationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        side = _side(request)
        conversation = _conversation(request, pk, side)
        return Response({
            **ConversationSerializer(conversation, context={'side': side, 'request': request}).data,
            'policy': _policy(),
        })


class MessageListView(APIView):
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [ChatSendThrottle()] if self.request.method == 'POST' else super().get_throttles()

    def get(self, request, pk):
        side = _side(request)
        conversation = _conversation(request, pk, side)
        qs = Message.objects.filter(conversation=conversation).select_related('sender', 'product').order_by('-id')
        before = request.query_params.get('before')
        if before and before.isdigit():
            qs = qs.filter(id__lt=int(before))
        rows = list(qs[:HISTORY_PAGE + 1])
        has_more = len(rows) > HISTORY_PAGE
        rows = rows[:HISTORY_PAGE][::-1]
        return Response({
            'results': MessageSerializer(rows, many=True, context={'request': request}).data,
            'has_more': has_more,
        })

    def post(self, request, pk):
        side = _side(request)
        conversation = _conversation(request, pk, side)
        product = _product(request.data.get('product_id'))
        if product is not None and product.vendor_id != conversation.vendor_id:
            product = None
        try:
            message, created = services.send_message(
                conversation, request.user, side, request.data.get('body'),
                product=product, client_id=str(request.data.get('client_id') or ''),
            )
        except services.ChatError as exc:
            return _error(exc)
        return Response(MessageSerializer(message, context={'request': request}).data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class MarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        side = _side(request)
        conversation = _conversation(request, pk, side)
        services.mark_read(conversation, side)
        return Response({'unread': 0, 'last_read_id': conversation.last_read_id_for(side)})


class UnreadCountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        side = _side(request)
        count = services.conversations_for(request.user, side).filter(**{f'{side}_unread__gt': 0}).count()
        return Response({'conversations': count})


class StoreConversationView(APIView):
    """The storefront's "Contact seller" panel: existing chat with this store, if any."""
    permission_classes = [IsAuthenticated]

    def get(self, request, slug):
        vendor = get_object_or_404(Vendor.objects.select_related('about'), slug=slug)
        conversation = (Conversation.objects.select_related('customer', 'vendor__about', 'product')
                        .filter(customer=request.user, vendor=vendor).first())
        membership = get_membership(request.user)
        muted_until = services.muted_until(request.user)
        return Response({
            'conversation': (ConversationSerializer(conversation, context={'side': Side.CUSTOMER, 'request': request}).data
                             if conversation and conversation.last_message_at else None),
            'accepting_messages': services.store_accepts_messages(vendor),
            'own_store': bool(membership and membership.vendor_id == vendor.pk),
            'muted_until': muted_until.isoformat() if muted_until else None,
            'policy': _policy(),
        })


class StorePresenceThrottle(AnonRateThrottle):
    scope = 'chat_presence'
    rate = '120/min'


class StorePresenceView(APIView):
    """
    "Online" / "Active 5 min ago" on the public store page. Only for stores that
    take messages: for the rest there's nothing to act on, so nothing is shown.
    last_seen is the later of the chat socket's last ping and the store's last
    dashboard request (Vendor.last_seen_at), so it still says something for a
    store whose team hasn't had the dashboard open since chat presence began.
    """
    permission_classes = [AllowAny]
    throttle_classes = [StorePresenceThrottle]

    def get(self, request, slug):
        vendor = get_object_or_404(Vendor, slug=slug)
        if not services.store_accepts_messages(vendor):
            return Response({'accepting_messages': False, 'online': False, 'last_seen': None})
        online, seen = realtime.presence(Side.VENDOR, vendor.pk)
        times = [t for t in (seen and parse_datetime(seen), vendor.last_seen_at) if t]
        last_seen = max(times).isoformat() if times else None
        return Response({'accepting_messages': True, 'online': online, 'last_seen': last_seen})

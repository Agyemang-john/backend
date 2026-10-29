"""
chat/serializers.py
Every payload is written from one side's point of view (context['side']): the
"counterpart" is the store for a shopper and the shopper for a store.

Shoppers appear to stores as "First L." with initials only, and stores never
see a shopper's email or phone: nothing in a chat payload helps take the
conversation elsewhere.
"""

from rest_framework import serializers

from . import realtime
from .models import Conversation, Message, Side


def media_url(field, request=None):
    """Absolute with a request; otherwise the storage URL (absolute on S3, /media/... locally)."""
    try:
        url = field.url if field else None
    except ValueError:
        return None
    if url and request is not None and url.startswith('/'):
        return request.build_absolute_uri(url)
    return url


def product_card(product, request=None):
    if product is None:
        return None
    return {'id': product.pk, 'title': product.title, 'sku': product.sku, 'slug': product.slug,
            'image': media_url(product.image, request)}


def customer_display(user):
    if user is None:
        return 'Negromart customer', 'N'
    first = (user.first_name or '').strip().title()
    last = (user.last_name or '').strip()
    name = f"{first} {last[:1].upper()}.".strip() if first else 'Negromart customer'
    return name, (f"{first[:1]}{last[:1]}".upper() or 'N')


class MessageSerializer(serializers.ModelSerializer):
    conversation_id = serializers.IntegerField(read_only=True)
    side = serializers.CharField(source='sender_side', read_only=True)
    sender_name = serializers.SerializerMethodField()
    product = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ['id', 'conversation_id', 'side', 'body', 'created_at', 'client_id', 'sender_name', 'product']

    def get_sender_name(self, obj):
        # Shoppers see which team member answered ("Ama"), first name only.
        if obj.sender_side == Side.VENDOR and obj.sender is not None:
            return (obj.sender.first_name or '').strip().title()
        return ''

    def get_product(self, obj):
        return product_card(obj.product, self.context.get('request'))


class ConversationSerializer(serializers.ModelSerializer):
    unread = serializers.SerializerMethodField()
    other_last_read_id = serializers.SerializerMethodField()
    other_delivered_id = serializers.SerializerMethodField()
    counterpart = serializers.SerializerMethodField()
    product = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ['id', 'reason', 'created_at', 'last_message_at', 'last_message_preview', 'last_sender_side',
                  'last_message_id', 'unread', 'other_last_read_id', 'other_delivered_id', 'counterpart', 'product']

    @property
    def side(self):
        return self.context['side']

    def get_unread(self, obj):
        return obj.unread_for(self.side)

    def get_other_last_read_id(self, obj):
        return obj.last_read_id_for(Side.other(self.side))

    def get_other_delivered_id(self, obj):
        return obj.delivered_id_for(Side.other(self.side))

    def get_counterpart(self, obj):
        presence = self.context.get('presence')
        if self.side == Side.CUSTOMER:
            vendor = obj.vendor
            about = getattr(vendor, 'about', None)
            online, last_seen = presence.get(obj.pk) if presence else realtime.presence(Side.VENDOR, vendor.pk)
            return {
                'kind': 'store', 'name': vendor.name, 'slug': vendor.slug,
                'avatar': media_url(about.profile_image, self.context.get('request')) if about else None,
                'initials': (vendor.name or 'S')[:1].upper(), 'online': online, 'last_seen': last_seen,
            }
        name, initials = customer_display(obj.customer)
        online, last_seen = presence.get(obj.pk) if presence else realtime.presence(Side.CUSTOMER, obj.customer_id)
        return {'kind': 'shopper', 'name': name, 'slug': None, 'avatar': None, 'initials': initials,
                'online': online, 'last_seen': last_seen}

    def get_product(self, obj):
        return product_card(obj.product, self.context.get('request'))


def serialize_conversations(conversations, side, request=None):
    """List payload with presence looked up in one cache round trip."""
    conversations = list(conversations)
    if side == Side.CUSTOMER:
        presence = realtime.presence_many(Side.VENDOR, {c.pk: c.vendor_id for c in conversations})
    else:
        presence = realtime.presence_many(Side.CUSTOMER, {c.pk: c.customer_id for c in conversations})
    return ConversationSerializer(conversations, many=True,
                                  context={'side': side, 'request': request, 'presence': presence}).data

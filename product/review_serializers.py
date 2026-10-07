"""
product/review_serializers.py
Public shape of reviews and their media for the product page and seller views.
"""

from rest_framework import serializers

from .models import ProductReview, ReviewMedia


def _absolute(request, field):
    if not field:
        return None
    url = field.url
    return request.build_absolute_uri(url) if request and url.startswith('/') else url


class ReviewMediaSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = ReviewMedia
        fields = ['id', 'kind', 'url', 'thumbnail_url', 'width', 'height']

    def get_url(self, obj):
        return _absolute(self.context.get('request'), obj.file)

    def get_thumbnail_url(self, obj):
        return _absolute(self.context.get('request'), obj.thumbnail)


class ReviewAuthorSerializer(serializers.Serializer):
    """First name + last initial only; reviews are public."""
    def to_representation(self, user):
        if not user:
            return {'display_name': 'Negromart customer', 'initials': 'N'}
        first = (user.first_name or '').strip()
        last = (user.last_name or '').strip()
        return {
            'display_name': f"{first} {last[:1]}.".strip() if first else 'Negromart customer',
            'initials': f"{first[:1]}{last[:1]}".upper() or 'N',
        }


class PublicReviewSerializer(serializers.ModelSerializer):
    author = ReviewAuthorSerializer(source='user', read_only=True)
    media = serializers.SerializerMethodField()
    voted_helpful = serializers.SerializerMethodField()

    class Meta:
        model = ProductReview
        # No moderation fields: public responses never carry internal notes.
        fields = ['id', 'rating', 'title', 'review', 'date', 'author', 'is_verified_purchase', 'purchased_variant',
                  'helpful_count', 'voted_helpful', 'media', 'seller_reply', 'seller_replied_at']

    def get_media(self, obj):
        # Uses the prefetched, visible media (see review_views.visible_reviews).
        items = getattr(obj, 'visible_media', None)
        if items is None:
            items = [m for m in obj.media.all() if not m.is_hidden]
        return ReviewMediaSerializer(items, many=True, context=self.context).data

    def get_voted_helpful(self, obj):
        voted = self.context.get('voted_ids')
        return bool(voted and obj.id in voted)


class ReviewInputSerializer(serializers.Serializer):
    """
    What a customer may send when writing or editing a review. Anything else
    (status, verification, user, order, counts) is ignored: those are set by
    product/review_services.py from trusted data only.
    """
    rating = serializers.IntegerField(min_value=1, max_value=5)
    title = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')
    review = serializers.CharField(min_length=10, max_length=1000)
    media_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False,
                                      max_length=7, default=list)


class OwnReviewSerializer(PublicReviewSerializer):
    """The author's view: includes where their review stands, never the staff's internal reason."""
    moderation_status = serializers.CharField(read_only=True)
    status_label = serializers.SerializerMethodField()
    product = serializers.SerializerMethodField()

    class Meta(PublicReviewSerializer.Meta):
        fields = PublicReviewSerializer.Meta.fields + ['moderation_status', 'status_label', 'product', 'updated']

    STATUS_LABELS = {
        'approved': 'Published',
        'pending': 'Awaiting moderation',
        'rejected': 'Not published: edit and resubmit',
        'hidden': 'Hidden by Negromart',
    }

    def get_status_label(self, obj):
        return self.STATUS_LABELS.get(obj.moderation_status, obj.moderation_status)

    def get_product(self, obj):
        product = obj.product
        if not product:
            return None
        return {
            'id': product.id, 'title': product.title, 'slug': product.slug, 'sku': product.sku,
            'image': _absolute(self.context.get('request'), product.image),
        }


class StaffReviewSerializer(OwnReviewSerializer):
    """Moderation queue: everything a moderator needs to decide."""
    customer_email = serializers.EmailField(source='user.email', read_only=True, default=None)
    order_number = serializers.CharField(source='order_item.order.order_number', read_only=True, default=None)
    moderated_by_email = serializers.EmailField(source='moderated_by.email', read_only=True, default=None)

    class Meta(OwnReviewSerializer.Meta):
        fields = OwnReviewSerializer.Meta.fields + [
            'customer_email', 'order_number', 'moderation_flags', 'moderation_reason',
            'moderated_at', 'moderated_by_email',
        ]


class ModerationEventSerializer(serializers.Serializer):
    from_status = serializers.CharField()
    to_status = serializers.CharField()
    actor = serializers.SerializerMethodField()
    reason = serializers.CharField()
    flags = serializers.ListField()
    created_at = serializers.DateTimeField()

    def get_actor(self, obj):
        return obj.actor.email if obj.actor else 'Automatic checks'

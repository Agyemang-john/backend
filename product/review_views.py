"""
product/review_views.py
Review API. HTTP only; the rules are in product/review_services.py.

Public
    GET    /api/v1/product/<id>/reviews/?rating=&media=1&sort=&page=   approved reviews, paginated
    GET    /api/v1/product/<id>/reviews/summary/                       approved-only statistics (cached)
Customer
    POST   /api/v1/product/<id>/reviews/                               write a review
    GET    /api/v1/product/<id>/reviews/eligibility/                   may I review this?
    GET    /api/v1/product/reviews/mine/                               my reviews, any status
    PATCH  /api/v1/product/reviews/<id>/                               edit my review
    DELETE /api/v1/product/reviews/<id>/                               delete my review
    POST   /api/v1/product/reviews/media/                              upload one photo/video
    DELETE /api/v1/product/reviews/media/<id>/                         discard an unattached upload
    POST   /api/v1/product/reviews/<id>/helpful/  (DELETE to undo)     helpful vote
Staff (is_staff + product.moderate_productreview, or superuser)
    GET    /api/v1/product/reviews/moderation/?status=pending          moderation queue
    POST   /api/v1/product/reviews/<id>/moderate/                      {action: approve|reject|hide, reason}
    GET    /api/v1/product/reviews/<id>/history/                       moderation log

Only APPROVED reviews reach public lists and statistics. Lists are filtered
and sorted in SQL on indexed columns; the summary is one cached aggregate per
product, invalidated by signals (product/signals.py).
"""

import logging

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Prefetch, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from . import review_media
from . import review_services as services
from .models import ProductReview, ReviewHelpfulVote, ReviewMedia
from .review_serializers import (
    ModerationEventSerializer, OwnReviewSerializer, PublicReviewSerializer, ReviewInputSerializer,
    ReviewMediaSerializer, StaffReviewSerializer,
)

logger = logging.getLogger('reviews')

SUMMARY_CACHE_SECONDS = 600
GALLERY_SIZE = 12

SORTS = {
    'helpful': ('-helpful_count', '-date'),
    'recent': ('-date',),
    'rating_high': ('-rating', '-helpful_count', '-date'),
    'rating_low': ('rating', '-helpful_count', '-date'),
}


def summary_cache_key(product_id):
    return f"review_summary:v2:{product_id}"


def _with_media(qs):
    return qs.select_related('user', 'product').prefetch_related(Prefetch(
        'media', queryset=ReviewMedia.objects.filter(is_hidden=False), to_attr='visible_media',
    ))


def visible_reviews(product_id):
    """Approved reviews for a product with author and visible media loaded."""
    return _with_media(ProductReview.objects.filter(product_id=product_id,
                                                    moderation_status=ProductReview.APPROVED))


def _error(exc: services.ReviewError):
    return Response({'detail': str(exc), 'code': exc.code}, status=exc.status)


# ── Throttles ─────────────────────────────────────────────────────────────────

class ReviewWriteThrottle(UserRateThrottle):
    scope = 'review_write'
    rate = '20/hour'


class ReviewMediaThrottle(UserRateThrottle):
    scope = 'review_media'
    rate = '40/hour'


class HelpfulVoteThrottle(UserRateThrottle):
    scope = 'review_helpful'
    rate = '120/hour'


# ── Public + create ───────────────────────────────────────────────────────────

class ReviewPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 30


class ProductReviewListView(APIView):
    """GET: approved reviews. POST: write a review (signed-in, eligible customers)."""

    def get_permissions(self):
        return [IsAuthenticated()] if self.request.method == 'POST' else [AllowAny()]

    def get_throttles(self):
        return [ReviewWriteThrottle()] if self.request.method == 'POST' else super().get_throttles()

    def get(self, request, product_id):
        qs = visible_reviews(product_id)
        rating = request.query_params.get('rating')
        if rating in {'1', '2', '3', '4', '5'}:
            qs = qs.filter(rating=int(rating))
        if request.query_params.get('media') in ('1', 'true'):
            qs = qs.filter(has_media=True)
        qs = qs.order_by(*SORTS.get(request.query_params.get('sort'), SORTS['helpful']))

        paginator = ReviewPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        voted_ids = set()
        if request.user.is_authenticated and page:
            voted_ids = set(ReviewHelpfulVote.objects.filter(
                user=request.user, review_id__in=[r.id for r in page],
            ).values_list('review_id', flat=True))
        data = PublicReviewSerializer(page, many=True, context={'request': request, 'voted_ids': voted_ids}).data
        return paginator.get_paginated_response(data)

    def post(self, request, product_id):
        payload = ReviewInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            review = services.create_review(request.user, product_id, payload.validated_data)
        except services.ReviewError as exc:
            return _error(exc)
        review = _with_media(ProductReview.objects.filter(pk=review.pk)).get()
        return Response(OwnReviewSerializer(review, context={'request': request}).data,
                        status=status.HTTP_201_CREATED)


class ReviewEligibilityView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, product_id):
        from .models import Product
        product = Product.objects.filter(pk=product_id).only('id', 'status').first()
        result = services.eligibility(request.user, product)
        return Response({'can_review': result.can_review, 'reason': result.reason,
                         'message': result.message, 'review_id': result.review_id})


def build_summary(product_id, request=None):
    cached = cache.get(summary_cache_key(product_id))
    if cached is not None:
        return cached

    approved = ProductReview.objects.filter(product_id=product_id, moderation_status=ProductReview.APPROVED)
    agg = approved.aggregate(
        count=Count('id'),
        with_media=Count('id', filter=Q(has_media=True)),
        **{f'r{n}': Count('id', filter=Q(rating=n)) for n in range(1, 6)},
    )
    total = agg['count'] or 0
    average = (sum(n * agg[f'r{n}'] for n in range(1, 6)) / total) if total else 0
    gallery = (
        ReviewMedia.objects.filter(review__product_id=product_id, review__moderation_status=ProductReview.APPROVED,
                                   is_hidden=False)
        .select_related('review').order_by('-review__helpful_count', '-created_at')[:GALLERY_SIZE]
    )
    summary = {
        'count': total,
        'average': round(average, 2),
        'distribution': {str(n): agg[f'r{n}'] for n in range(1, 6)},
        'percentages': {str(n): (round(agg[f'r{n}'] * 100 / total) if total else 0) for n in range(1, 6)},
        'with_media_count': agg['with_media'],
        'gallery': [
            {**ReviewMediaSerializer(m, context={'request': request}).data, 'review_id': m.review_id,
             'rating': m.review.rating}
            for m in gallery
        ],
    }
    cache.set(summary_cache_key(product_id), summary, SUMMARY_CACHE_SECONDS)
    return summary


class ProductReviewSummaryView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, product_id):
        return Response(build_summary(product_id, request))


# ── The customer's own reviews ────────────────────────────────────────────────

class MyReviewsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _with_media(ProductReview.objects.filter(user=request.user)).order_by('-date')
        paginator = ReviewPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(OwnReviewSerializer(page, many=True, context={'request': request}).data)


class ReviewDetailView(APIView):
    """PATCH/DELETE the signed-in customer's own review."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ReviewWriteThrottle]

    def _own(self, request, review_id):
        # 404 (not 403) for other people's reviews: don't confirm they exist.
        return get_object_or_404(ProductReview.objects.select_related('product'), pk=review_id, user=request.user)

    def patch(self, request, review_id):
        review = self._own(request, review_id)
        payload = ReviewInputSerializer(data={
            'rating': request.data.get('rating', review.rating),
            'title': request.data.get('title', review.title),
            'review': request.data.get('review', review.review),
        })
        payload.is_valid(raise_exception=True)
        try:
            services.update_review(review, request.user, payload.validated_data)
        except services.ReviewError as exc:
            return _error(exc)
        review = _with_media(ProductReview.objects.filter(pk=review.pk)).get()
        return Response(OwnReviewSerializer(review, context={'request': request}).data)

    def delete(self, request, review_id):
        review = self._own(request, review_id)
        try:
            services.delete_review(review, request.user)
        except services.ReviewError as exc:
            return _error(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Uploads ───────────────────────────────────────────────────────────────────

class ReviewMediaUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]
    throttle_classes = [ReviewMediaThrottle]

    def post(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            return Response({'detail': 'Choose a photo or video to upload.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            kind = review_media.detect_kind(upload)
            media = ReviewMedia(uploaded_by=request.user, kind=kind, size_bytes=upload.size)
            if kind == ReviewMedia.KIND_IMAGE:
                full, thumb, (media.width, media.height) = review_media.process_image(upload)
                media.file.save('photo.jpg', full, save=False)
                media.thumbnail.save('thumb.jpg', thumb, save=False)
            else:
                review_media.check_video(upload)
                media.file.save(upload.name, upload, save=False)
        except review_media.MediaError as exc:
            logger.info("review media rejected for user=%s: %s", request.user.pk, exc)
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        media.save()
        return Response(ReviewMediaSerializer(media, context={'request': request}).data,
                        status=status.HTTP_201_CREATED)


class ReviewMediaDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, media_id):
        media = get_object_or_404(ReviewMedia, pk=media_id, uploaded_by=request.user, review__isnull=True)
        delete_media_files(media)
        media.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def delete_media_files(media):
    for field in (media.file, media.thumbnail):
        if field:
            field.delete(save=False)


def attach_media(review, user, media_ids):
    """Attach the user's own unattached uploads to their new review, within the per-review limits."""
    ids = [int(i) for i in (media_ids or []) if str(i).isdigit()]
    if not ids:
        return 0
    items = list(ReviewMedia.objects.filter(pk__in=ids, uploaded_by=user, review__isnull=True))
    images = [m for m in items if m.kind == ReviewMedia.KIND_IMAGE][:review_media.MAX_IMAGES_PER_REVIEW]
    videos = [m for m in items if m.kind == ReviewMedia.KIND_VIDEO][:review_media.MAX_VIDEOS_PER_REVIEW]
    order = {mid: pos for pos, mid in enumerate(ids)}
    for media in images + videos:
        media.review = review
        media.position = order.get(media.pk, 0)
    ReviewMedia.objects.bulk_update(images + videos, ['review', 'position'])
    if images or videos:
        ProductReview.objects.filter(pk=review.pk).update(has_media=True)
        review.has_media = True
    return len(images) + len(videos)


# ── Helpful votes ─────────────────────────────────────────────────────────────

class ReviewHelpfulView(APIView):
    """One vote per customer per review; voting again is a no-op, DELETE removes it."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [HelpfulVoteThrottle]

    def post(self, request, review_id):
        review = get_object_or_404(ProductReview, pk=review_id, moderation_status=ProductReview.APPROVED)
        if review.user_id == request.user.pk:
            return Response({'detail': "You can't vote on your own review."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                ReviewHelpfulVote.objects.create(review=review, user=request.user)
                ProductReview.objects.filter(pk=review.pk).update(helpful_count=F('helpful_count') + 1)
        except IntegrityError:
            pass  # already voted
        review.refresh_from_db(fields=['helpful_count'])
        return Response({'helpful_count': review.helpful_count, 'voted_helpful': True})

    def delete(self, request, review_id):
        review = get_object_or_404(ProductReview, pk=review_id)
        with transaction.atomic():
            deleted, _ = ReviewHelpfulVote.objects.filter(review=review, user=request.user).delete()
            if deleted:
                ProductReview.objects.filter(pk=review.pk, helpful_count__gt=0).update(
                    helpful_count=F('helpful_count') - 1)
        review.refresh_from_db(fields=['helpful_count'])
        return Response({'helpful_count': review.helpful_count, 'voted_helpful': False})


# ── Staff moderation ──────────────────────────────────────────────────────────

class CanModerateReviews(BasePermission):
    message = 'You do not have permission to moderate reviews.'

    def has_permission(self, request, view):
        return services.can_moderate(request.user)


class ModerationQueueView(APIView):
    permission_classes = [IsAuthenticated, CanModerateReviews]

    def get(self, request):
        wanted = request.query_params.get('status', ProductReview.PENDING)
        qs = ProductReview.objects.all()
        if wanted in dict(ProductReview.MODERATION_CHOICES):
            qs = qs.filter(moderation_status=wanted)
        if request.query_params.get('product'):
            qs = qs.filter(product__title__icontains=request.query_params['product'])
        qs = _with_media(qs.select_related('order_item__order', 'moderated_by')).order_by('date')
        paginator = ReviewPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            StaffReviewSerializer(page, many=True, context={'request': request}).data)


class ModerateReviewView(APIView):
    permission_classes = [IsAuthenticated, CanModerateReviews]

    def post(self, request, review_id):
        review = get_object_or_404(ProductReview, pk=review_id)
        try:
            services.moderate(review, request.data.get('action'), request.user, request.data.get('reason', ''))
        except services.ReviewError as exc:
            return _error(exc)
        review = _with_media(ProductReview.objects.filter(pk=review.pk)).get()
        return Response(StaffReviewSerializer(review, context={'request': request}).data)


class ReviewHistoryView(APIView):
    permission_classes = [IsAuthenticated, CanModerateReviews]

    def get(self, request, review_id):
        review = get_object_or_404(ProductReview, pk=review_id)
        events = review.moderation_events.select_related('actor')
        return Response(ModerationEventSerializer(events, many=True).data)

"""
product/review_services.py
Business rules for product reviews. Views and admin call these; nothing else
creates, edits, moderates or deletes reviews.

Eligibility (all checked server-side, from the authenticated user):
    signed in · product exists and is published · the user owns an order that
    contains this product · that order line was delivered (shipment delivered
    or whole order delivered; payment alone is not enough) and not cancelled ·
    no existing review by this user for this product.

Policy: one review per customer per product, however many times it was
bought; the most recent delivered line is recorded as evidence (order_item).
A later refund or return does not remove verification: the customer did buy
and receive the product.

Moderation (independent of the purchase check and of the rating):
    clean text  → APPROVED (public at once)
    any flag    → PENDING  (staff queue; see review_moderation.py)
    staff       → approve / reject / hide, with an internal reason
    edits       → re-checked; hidden or rejected reviews return to PENDING
Every status change is written to ReviewModerationEvent.
"""

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import review_moderation
from .models import Product, ProductReview, ReviewMedia, ReviewModerationEvent

logger = logging.getLogger('reviews')

RATING_RANGE = range(1, 6)


class ReviewError(Exception):
    """A rule was broken; `code` is stable for clients, message is safe to show."""

    def __init__(self, message, code='invalid', status=400):
        super().__init__(message)
        self.code = code
        self.status = status


# ── Eligibility ───────────────────────────────────────────────────────────────

def purchased_line(user, product):
    """The user's most recent delivered, non-cancelled order line for this product, or None."""
    from order.models import OrderProduct
    return (
        OrderProduct.objects.filter(order__user=user, order__is_ordered=True, product=product)
        .filter(Q(delivered_date__isnull=False) | Q(order__status='delivered'))
        .exclude(status='canceled')
        .exclude(order__status='canceled')
        .select_related('variant__size', 'variant__color')
        .order_by('-date_created')
        .first()
    )


def describe_variant(line):
    variant = line.variant if line else None
    if not variant:
        return ''
    parts = [getattr(variant.size, 'name', None), getattr(variant.color, 'name', None)]
    return ' · '.join(p for p in parts if p) or (variant.title or '')[:120]


@dataclass
class Eligibility:
    can_review: bool
    reason: str = ''          # sign_in | not_available | already_reviewed | not_delivered
    message: str = ''
    review_id: int | None = None


def eligibility(user, product):
    """What the customer may do. Reveals nothing about anyone else's orders."""
    if not user or not user.is_authenticated:
        return Eligibility(False, 'sign_in', 'Sign in to review this product.')
    if product is None or product.status != 'published':
        return Eligibility(False, 'not_available', 'This product cannot be reviewed.')
    existing = ProductReview.objects.filter(user=user, product=product).only('pk').first()
    if existing:
        return Eligibility(False, 'already_reviewed', "You've already reviewed this product.", existing.pk)
    if purchased_line(user, product) is None:
        return Eligibility(False, 'not_delivered',
                           'You can review this product after an order containing it has been delivered to you.')
    return Eligibility(True)


# ── Validation helpers ────────────────────────────────────────────────────────

def _clean(data):
    rating = data.get('rating')
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        raise ReviewError('Choose a rating from 1 to 5 stars.', 'invalid_rating')
    if rating not in RATING_RANGE:
        raise ReviewError('Choose a rating from 1 to 5 stars.', 'invalid_rating')
    title = (data.get('title') or '').strip()
    text = (data.get('review') or data.get('comment') or '').strip()
    if len(title) > 120:
        raise ReviewError('Keep the title under 120 characters.', 'invalid_title')
    if len(text) < 10:
        raise ReviewError('Write at least 10 characters about the product.', 'invalid_text')
    if len(text) > 1000:
        raise ReviewError('Reviews can be up to 1000 characters.', 'invalid_text')
    return rating, title, text


def _set_status(review, new_status, *, actor=None, reason='', flags=None):
    old = review.moderation_status
    review.moderation_status = new_status
    if actor is not None:
        review.moderated_by = actor
        review.moderated_at = timezone.now()
    if reason:
        review.moderation_reason = reason
    if flags is not None:
        review.moderation_flags = flags
    review.save()
    ReviewModerationEvent.objects.create(review=review, from_status=old, to_status=new_status,
                                         actor=actor, reason=reason, flags=flags or [])
    logger.info("review %s: %s -> %s by %s flags=%s", review.pk, old or '-', new_status,
                getattr(actor, 'pk', 'auto'), flags or [])


def _auto_moderate(review, *, has_media, resubmission=False):
    """Run the automatic checks and record the outcome."""
    flags = review_moderation.run_checks(review, has_media=has_media)
    target = ProductReview.PENDING if (flags or resubmission) else ProductReview.APPROVED
    if (target == review.moderation_status and flags == review.moderation_flags
            and review.moderation_events.exists()):
        return  # re-checked, nothing changed: no audit noise
    reason = 'Edited after moderation; needs another look.' if resubmission and not flags else ''
    _set_status(review, target, flags=flags, reason=reason)


# ── Customer actions ──────────────────────────────────────────────────────────

def create_review(user, product_id, data):
    """Create (and auto-moderate) a review. Returns the review."""
    from .review_views import attach_media  # media helpers live with the upload views

    product = Product.objects.filter(pk=product_id).first()
    rating, title, text = _clean(data)
    check = eligibility(user, product)
    if not check.can_review:
        status = 409 if check.reason == 'already_reviewed' else (401 if check.reason == 'sign_in' else 403)
        raise ReviewError(check.message, check.reason, status)

    line = purchased_line(user, product)
    try:
        with transaction.atomic():
            review = ProductReview.objects.create(
                user=user, product=product, vendor=product.vendor, order_item=line,
                rating=rating, title=title, review=text,
                is_verified_purchase=True, purchased_variant=describe_variant(line),
                moderation_status=ProductReview.PENDING,
                content_hash=review_moderation.content_hash(title, text),
            )
            media_ids = data.get('media_ids') or []
            attached = attach_media(review, user, media_ids if isinstance(media_ids, list) else [])
            _auto_moderate(review, has_media=bool(attached))
    except IntegrityError:
        # A concurrent request created it first (uniq_review_per_user_product).
        raise ReviewError("You've already reviewed this product.", 'already_reviewed', 409)

    from .review_reminders import mark_reviewed
    mark_reviewed(user, product)
    _notify(review, created=True)
    return review


def update_review(review, user, data):
    """Owner edits rating/title/text. Content is re-checked; purchase is re-verified."""
    if review.user_id != user.pk:
        raise ReviewError('You can only edit your own reviews.', 'forbidden', 403)
    merged = {'rating': data.get('rating', review.rating), 'title': data.get('title', review.title),
              'review': data.get('review', data.get('comment', review.review))}
    rating, title, text = _clean(merged)
    line = purchased_line(user, review.product) if review.product else None
    if line is None:
        raise ReviewError('This review can no longer be edited.', 'not_delivered', 403)

    was = review.moderation_status
    with transaction.atomic():
        review.rating, review.title, review.review = rating, title, text
        review.order_item = line
        review.content_hash = review_moderation.content_hash(title, text)
        review.save()
        has_media = ReviewMedia.objects.filter(review=review, is_hidden=False).exists()
        # Reviews staff took down must be looked at again by staff.
        _auto_moderate(review, has_media=has_media,
                       resubmission=was in (ProductReview.HIDDEN, ProductReview.REJECTED))
    if review.moderation_status != was:
        _notify(review, created=False)
    return review


def delete_review(review, user):
    """The author removes their own review (and its photos/videos)."""
    from .review_views import delete_media_files
    if review.user_id != user.pk:
        raise ReviewError('You can only delete your own reviews.', 'forbidden', 403)
    for media in review.media.all():
        delete_media_files(media)
    logger.info("review %s deleted by its author %s", review.pk, user.pk)
    review.delete()


# ── Staff moderation ──────────────────────────────────────────────────────────

MODERATION_ACTIONS = {
    'approve': ProductReview.APPROVED,
    'reject': ProductReview.REJECTED,
    'hide': ProductReview.HIDDEN,
}
_ALLOWED_FROM = {
    'approve': {ProductReview.PENDING, ProductReview.REJECTED, ProductReview.HIDDEN},
    'reject': {ProductReview.PENDING},
    'hide': {ProductReview.APPROVED, ProductReview.PENDING},
}


def can_moderate(user):
    return bool(user and user.is_authenticated and user.is_staff
                and (user.is_superuser or user.has_perm('product.moderate_productreview')))


def moderate(review, action, actor, reason=''):
    if not can_moderate(actor):
        raise ReviewError('You do not have permission to moderate reviews.', 'forbidden', 403)
    if action not in MODERATION_ACTIONS:
        raise ReviewError('Unknown moderation action.', 'invalid_action')
    if review.moderation_status not in _ALLOWED_FROM[action]:
        raise ReviewError(f"A {review.get_moderation_status_display().lower()} review can't be {action}d.",
                          'invalid_transition', 409)
    if action in ('reject', 'hide') and not (reason or '').strip():
        raise ReviewError('Record a reason for the moderation log.', 'reason_required')
    with transaction.atomic():
        locked = ProductReview.objects.select_for_update().get(pk=review.pk)
        _set_status(locked, MODERATION_ACTIONS[action], actor=actor, reason=(reason or '').strip())
    review.refresh_from_db()
    _notify(review, created=False)
    return review


# ── Notifications ─────────────────────────────────────────────────────────────

def _notify(review, *, created):
    """Tell the author what happened. Internal moderation reasons are never sent."""
    if not review.user_id:
        return
    product_title = review.product.title if review.product else 'the product'
    if review.moderation_status == ProductReview.APPROVED:
        verb, message = 'customer_review_published', f'Your review of {product_title} is now live. Thank you!'
    elif review.moderation_status == ProductReview.PENDING:
        verb, message = 'customer_review_pending', (
            f'Thanks for reviewing {product_title}. It will appear once our team has checked it.')
    elif review.moderation_status == ProductReview.REJECTED:
        verb, message = 'customer_review_rejected', (
            f"Your review of {product_title} wasn't published because it doesn't meet our review guidelines. "
            'You can edit it and submit it again.')
    else:
        return  # hidden: no customer message
    try:
        from notification.utils import send_notification
        send_notification(recipient=review.user, verb=verb, target=review, data={
            'message': message, 'review_id': review.pk, 'url': '/dashboard/reviews',
        })
    except Exception:
        logger.exception("review %s: notification failed", review.pk)

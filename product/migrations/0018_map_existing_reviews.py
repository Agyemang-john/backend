"""Data step for review moderation, in its own transaction (Postgres can't
build indexes in a transaction that has pending deferred-constraint triggers)."""

from django.db import migrations


def map_existing_reviews(apps, schema_editor):
    """
    Give existing reviews a moderation status without losing anything:
      published (status=True) → approved; unpublished → pending (staff decide)
      verified reviews get their purchase line recorded as order_item
      older duplicates of the same customer+product → hidden (kept, not deleted)
    """
    from django.db.models import Count, Q

    Review = apps.get_model('product', 'ProductReview')
    OrderProduct = apps.get_model('order', 'OrderProduct')

    Review.objects.filter(status=True).update(moderation_status='approved')
    Review.objects.filter(status=False).update(moderation_status='pending')

    for review in Review.objects.filter(is_verified_purchase=True, order_item__isnull=True).iterator():
        line = (
            OrderProduct.objects.filter(order__user_id=review.user_id, product_id=review.product_id,
                                        order__is_ordered=True)
            .filter(Q(delivered_date__isnull=False) | Q(order__status='delivered'))
            .order_by('-date_created').first()
        )
        if line:
            Review.objects.filter(pk=review.pk).update(order_item=line)

    dupes = (
        Review.objects.filter(user__isnull=False, product__isnull=False)
        .values('user_id', 'product_id').annotate(n=Count('id')).filter(n__gt=1)
    )
    for group in dupes:
        rows = list(Review.objects.filter(user_id=group['user_id'], product_id=group['product_id'])
                    .order_by('-date').values_list('pk', flat=True))
        Review.objects.filter(pk__in=rows[1:]).update(
            moderation_status='hidden', status=False,
            moderation_reason='Earlier duplicate of review %s (one review per product).' % rows[0],
        )



class Migration(migrations.Migration):

    dependencies = [
        ('product', '0017_review_moderation'),
        ('order', '0008_returns_and_delivery_providers'),
    ]

    operations = [
        migrations.RunPython(map_existing_reviews, migrations.RunPython.noop),
    ]

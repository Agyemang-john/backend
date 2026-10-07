"""
Mark existing reviews as verified purchases where the reviewer has a
delivered order for the product (the only way a review could be written).
"""

from django.db import migrations
from django.db.models import Exists, OuterRef


def forwards(apps, schema_editor):
    ProductReview = apps.get_model('product', 'ProductReview')
    OrderProduct = apps.get_model('order', 'OrderProduct')
    bought = OrderProduct.objects.filter(
        order__user=OuterRef('user'), product=OuterRef('product'),
        order__is_ordered=True, order__status='delivered',
    )
    ids = list(ProductReview.objects.annotate(bought=Exists(bought)).filter(bought=True).values_list('pk', flat=True))
    ProductReview.objects.filter(pk__in=ids).update(is_verified_purchase=True)


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0015_review_media_and_helpful_votes'),
        ('order', '0008_returns_and_delivery_providers'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

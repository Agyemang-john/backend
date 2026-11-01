"""Existing wishlists start watching prices, like new ones do (engagement/signals.py)."""
from django.db import migrations


def backfill(apps, schema_editor):
    Wishlist = apps.get_model('product', 'Wishlist')
    ProductAlert = apps.get_model('engagement', 'ProductAlert')
    batch = []
    rows = Wishlist.objects.values_list('user_id', 'product_id', 'product__price').order_by('pk')
    for user_id, product_id, price in rows.iterator(chunk_size=2000):
        batch.append(ProductAlert(user_id=user_id, product_id=product_id, kind='price', source='wishlist',
                                  reference_price=price, last_seen_price=price))
        if len(batch) >= 2000:
            ProductAlert.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        ProductAlert.objects.bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ('engagement', '0001_initial'),
        ('product', '0028_product_sourcing'),
    ]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]

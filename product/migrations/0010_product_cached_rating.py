from django.db import migrations, models


def backfill_product_ratings(apps, schema_editor):
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE product_product
            SET
                avg_rating = COALESCE(sub.avg_r, 0.0),
                review_count = COALESCE(sub.cnt, 0)
            FROM (
                SELECT product_id,
                       AVG(rating)::float AS avg_r,
                       COUNT(*)::integer AS cnt
                FROM product_productreview
                WHERE status = TRUE
                GROUP BY product_id
            ) sub
            WHERE product_product.id = sub.product_id
        """)


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0009_occasion_occasionsection'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='avg_rating',
            field=models.FloatField(default=0.0, db_index=True),
        ),
        migrations.AddField(
            model_name='product',
            name='review_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(
                fields=['sub_category', 'status', 'avg_rating'],
                name='product_cat_stat_avg_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(
                fields=['sub_category', 'status', 'date'],
                name='product_cat_stat_date_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(
                fields=['brand', 'status', 'price'],
                name='product_brand_stat_price_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(
                fields=['brand', 'status', 'avg_rating'],
                name='product_brand_stat_avg_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(
                fields=['status', 'avg_rating'],
                name='product_stat_avg_rating_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(
                fields=['status', 'price'],
                name='product_stat_price_idx',
            ),
        ),
        migrations.RunPython(backfill_product_ratings, migrations.RunPython.noop),
    ]

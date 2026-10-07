"""One review per customer per product (hidden legacy duplicates excluded; see 0018)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0018_map_existing_reviews'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='productreview',
            constraint=models.UniqueConstraint(condition=models.Q(('product__isnull', False), ('user__isnull', False), models.Q(('moderation_status', 'hidden'), _negated=True)), fields=('user', 'product'), name='uniq_review_per_user_product'),
        ),
    ]

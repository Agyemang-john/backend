from django.contrib.postgres.search import SearchVector
from django.db import migrations
from django.db.models import F


def rebuild(apps, schema_editor):
    # Existing vectors were built with the server's default_text_search_config,
    # which on the production host isn't 'english', so they never matched the
    # search endpoint's english-stemmed queries. Rebuild them all with the
    # explicit config (mirrors Product.save()).
    Product = apps.get_model('product', 'Product')
    Product.objects.update(
        search_vector=(
            SearchVector(F('title'), weight='A', config='english') +
            SearchVector(F('description'), weight='B', config='english') +
            SearchVector(F('features'), weight='C', config='english') +
            SearchVector(F('specifications'), weight='C', config='english')
        )
    )


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0020_review_reminders'),
    ]

    operations = [
        migrations.RunPython(rebuild, migrations.RunPython.noop),
    ]

"""
Run the rich-text sanitiser (core/html_sanitize.py) over products saved before
Product.save() cleaned them. Rows whose HTML is already clean are left alone.
"""

from django.db import migrations

FIELDS = ('features', 'description', 'specifications', 'delivery_returns')


def sanitize(apps, schema_editor):
    from core.html_sanitize import clean_html
    Product = apps.get_model('product', 'Product')
    for row in Product.objects.values('pk', *FIELDS).iterator(chunk_size=500):
        changed = {f: clean_html(row[f]) for f in FIELDS if row[f] and clean_html(row[f]) != row[f]}
        if changed:
            Product.objects.filter(pk=row['pk']).update(**changed)


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0026_variant_gallery'),
    ]

    operations = [
        migrations.RunPython(sanitize, migrations.RunPython.noop),
    ]

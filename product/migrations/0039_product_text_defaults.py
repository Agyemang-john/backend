# Description and features are optional: no placeholder text by default.

import django_ckeditor_5.fields
from django.db import migrations


def clear_placeholders(apps, schema_editor):
    """Products saved with the old defaults showed them to shoppers as real text."""
    Product = apps.get_model('product', 'Product')
    Product.objects.filter(description='I sell good products only').update(description='')
    Product.objects.filter(features='Black').update(features='')


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0038_variant_options_gin'),
    ]

    operations = [
        migrations.AlterField(
            model_name='product',
            name='description',
            field=django_ckeditor_5.fields.CKEditor5Field(blank=True, default='', null=True),
        ),
        migrations.AlterField(
            model_name='product',
            name='features',
            field=django_ckeditor_5.fields.CKEditor5Field(blank=True, default='', null=True),
        ),
        migrations.RunPython(clear_placeholders, migrations.RunPython.noop),
    ]

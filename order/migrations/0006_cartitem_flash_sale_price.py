from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('order', '0005_fix_relationship_on_delete'),
    ]

    operations = [
        migrations.AddField(
            model_name='cartitem',
            name='flash_sale_price',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=10,
                null=True,
                help_text='Locked-in flash sale price when item was first added during an active sale.',
            ),
        ),
    ]

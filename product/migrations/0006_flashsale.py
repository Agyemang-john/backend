import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0005_fix_relationship_on_delete'),
        ('vendor', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='FlashSale',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sale_price', models.DecimalField(decimal_places=2, max_digits=10)),
                ('original_price', models.DecimalField(decimal_places=2, max_digits=10)),
                ('start_time', models.DateTimeField(db_index=True)),
                ('end_time', models.DateTimeField(db_index=True)),
                ('max_quantity', models.PositiveIntegerField(blank=True, null=True, help_text='Cap on units sold at flash price. Leave blank for unlimited.')),
                ('sold_count', models.PositiveIntegerField(default=0)),
                ('label', models.CharField(
                    choices=[
                        ('lightning', 'Lightning Deal'),
                        ('limited', 'Limited Offer'),
                        ('clearance', 'Clearance'),
                        ('daily', 'Daily Deal'),
                    ],
                    default='lightning',
                    max_length=20,
                )),
                ('is_active', models.BooleanField(default=True, db_index=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('product', models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='flash_sales',
                    to='product.product',
                )),
                ('variant', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='flash_sales',
                    to='product.variants',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='flash_sales',
                    to='vendor.vendor',
                )),
            ],
            options={
                'ordering': ['end_time'],
            },
        ),
        migrations.AddIndex(
            model_name='flashsale',
            index=models.Index(fields=['is_active', 'start_time', 'end_time'], name='product_fla_is_acti_idx'),
        ),
    ]

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('address', '0002_initial'),
        ('order', '0004_add_partially_delivered_and_shipment_unique_together'),
        ('product', '0004_remove_product_product_pro_vendor__915e06_idx_and_more'),
        ('vendor', '0001_initial'),
    ]

    operations = [
        # Order.address: PROTECT → SET_NULL (was blocking user deletion via User→Address CASCADE)
        migrations.AlterField(
            model_name='order',
            name='address',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='address.address',
            ),
        ),
        # OrderProduct.product: CASCADE → SET_NULL (seller deletes product, order history preserved)
        migrations.AlterField(
            model_name='orderproduct',
            name='product',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='product.product',
            ),
        ),
        # Shipment.vendor: CASCADE → SET_NULL (vendor deleted, tracking records preserved)
        migrations.AlterField(
            model_name='shipment',
            name='vendor',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='vendor.vendor',
            ),
        ),
    ]

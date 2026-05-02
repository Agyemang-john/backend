import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('product', '0004_remove_product_product_pro_vendor__915e06_idx_and_more'),
        ('vendor', '0001_initial'),
        ('userauths', '0001_initial'),
    ]

    operations = [
        # Product.vendor: CASCADE → SET_NULL (vendor deleted, products stay for order history)
        migrations.AlterField(
            model_name='product',
            name='vendor',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='product',
                to='vendor.vendor',
            ),
        ),
        # Variants.size: CASCADE → SET_NULL (deleting "XL" size no longer wipes all XL variants)
        migrations.AlterField(
            model_name='variants',
            name='size',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='product.size',
            ),
        ),
        # Variants.color: CASCADE → SET_NULL (deleting "Red" color no longer wipes all red variants)
        migrations.AlterField(
            model_name='variants',
            name='color',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='product.color',
            ),
        ),
        # ProductReview.vendor: CASCADE → SET_NULL (reviews survive vendor deletion)
        migrations.AlterField(
            model_name='productreview',
            name='vendor',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='product_reviews',
                to='vendor.vendor',
            ),
        ),
        # ProductDeliveryOption.delivery_option: CASCADE → PROTECT
        # (prevents deleting a delivery option that products are actively using)
        migrations.AlterField(
            model_name='productdeliveryoption',
            name='delivery_option',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to='product.deliveryoption',
            ),
        ),
    ]

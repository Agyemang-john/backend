from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vendor', '0005_vendor_shop_paused_vendor_shop_paused_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='vendor',
            name='notification_prefs',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Vendor notification preferences, e.g. {new_order: true, marketing: false}.',
            ),
        ),
    ]

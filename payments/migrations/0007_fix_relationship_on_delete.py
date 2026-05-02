import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0006_emailtemplate_subscriptionemailconfig'),
        ('vendor', '0001_initial'),
        ('userauths', '0001_initial'),
    ]

    operations = [
        # Payment.user: CASCADE → SET_NULL (payment audit record survives user deletion)
        migrations.AlterField(
            model_name='payment',
            name='user',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='payments',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Payout.vendor: CASCADE → SET_NULL (payout history survives vendor deletion)
        migrations.AlterField(
            model_name='payout',
            name='vendor',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='vendor.vendor',
            ),
        ),
        # PaymentTransaction.vendor: CASCADE → SET_NULL (transaction audit trail survives vendor deletion)
        migrations.AlterField(
            model_name='paymenttransaction',
            name='vendor',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='transactions',
                to='vendor.vendor',
            ),
        ),
    ]

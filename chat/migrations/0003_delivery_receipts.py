# Delivery receipts (✓✓ grey) for shopper ↔ seller chat.

from django.db import migrations, models
from django.db.models import F


def seed_from_read(apps, schema_editor):
    # Anything already read was delivered.
    Conversation = apps.get_model('chat', 'Conversation')
    Conversation.objects.update(customer_delivered_id=F('customer_last_read_id'),
                                vendor_delivered_id=F('vendor_last_read_id'))


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0002_reply_reminders'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='customer_delivered_id',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='conversation',
            name='vendor_delivered_id',
            field=models.BigIntegerField(default=0),
        ),
        migrations.RunPython(seed_from_read, migrations.RunPython.noop),
    ]

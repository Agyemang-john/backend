"""
Starting SMS allowance for messages to followers, by tier (texts per month,
one per shopper reached). Admins can change it per plan.

    basic       0 (app and email only)
    pro         300
    enterprise  2000
"""
from django.db import migrations

DEFAULTS = {'pro': 300, 'enterprise': 2000}


def apply_defaults(apps, schema_editor):
    SubscriptionPlan = apps.get_model('payments', 'SubscriptionPlan')
    for tier, limit in DEFAULTS.items():
        SubscriptionPlan.objects.filter(tier=tier).update(broadcast_sms_monthly_limit=limit)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0020_subscriptionplan_broadcast_sms_monthly_limit'),
    ]

    operations = [
        migrations.RunPython(apply_defaults, migrations.RunPython.noop),
    ]

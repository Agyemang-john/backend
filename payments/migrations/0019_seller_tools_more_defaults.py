"""
Starting point for the second set of seller tools on existing plans, by tier.
Admins can change any of these per plan (Subscription plans › Seller tools).

    basic       labels & pick lists, chat helpers, store link & page
    pro         + demand insights, promotions autopilot, 4 follower messages a month
    enterprise  + API & stock sync, 12 follower messages a month
"""
from django.db import migrations

BASIC = dict(can_print_labels=True, can_use_chat_tools=True, can_use_store_link=True)
PRO = dict(BASIC, can_use_demand_insights=True, can_use_promotions=True, broadcasts_monthly_limit=4)
ENTERPRISE = dict(PRO, can_use_api=True, broadcasts_monthly_limit=12)

DEFAULTS = {'basic': BASIC, 'pro': PRO, 'enterprise': ENTERPRISE}


def apply_defaults(apps, schema_editor):
    SubscriptionPlan = apps.get_model('payments', 'SubscriptionPlan')
    for tier, values in DEFAULTS.items():
        SubscriptionPlan.objects.filter(tier=tier).update(**values)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0018_subscriptionplan_broadcasts_monthly_limit_and_more'),
    ]

    operations = [
        migrations.RunPython(apply_defaults, migrations.RunPython.noop),
    ]

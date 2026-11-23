"""
Starting point for the new seller tools on existing plans, by tier. Admins can
change any of these per plan (Subscription plans › Seller tools).

    basic       bulk edit, stock forecast, profit report, 30 AI rewrites a month
    pro         + price insights, auto-repricer, 200 AI rewrites a month
    enterprise  everything, 1000 AI rewrites a month
"""
from django.db import migrations

DEFAULTS = {
    'basic': dict(can_bulk_edit=True, can_use_stock_forecast=True, can_use_profit_report=True,
                  listing_assistant_monthly_limit=30),
    'pro': dict(can_bulk_edit=True, can_use_stock_forecast=True, can_use_profit_report=True,
                can_use_price_insights=True, can_use_auto_repricer=True, listing_assistant_monthly_limit=200),
    'enterprise': dict(can_bulk_edit=True, can_use_stock_forecast=True, can_use_profit_report=True,
                       can_use_price_insights=True, can_use_auto_repricer=True, listing_assistant_monthly_limit=1000),
}


def apply_defaults(apps, schema_editor):
    SubscriptionPlan = apps.get_model('payments', 'SubscriptionPlan')
    for tier, values in DEFAULTS.items():
        SubscriptionPlan.objects.filter(tier=tier).update(**values)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0016_subscriptionplan_can_bulk_edit_and_more'),
    ]

    operations = [
        migrations.RunPython(apply_defaults, migrations.RunPython.noop),
    ]

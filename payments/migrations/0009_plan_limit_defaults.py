"""
Give existing subscription plans sensible values for the two new plan limits.
They are only starting points: change them per plan in Django admin.

    tier         team members   CSV exports
    free              1             no
    basic             3             yes
    pro              10             yes
    enterprise       25             yes
"""

from django.db import migrations

DEFAULTS = {
    'free': (1, False),
    'basic': (3, True),
    'pro': (10, True),
    'enterprise': (25, True),
}


def forwards(apps, schema_editor):
    SubscriptionPlan = apps.get_model('payments', 'SubscriptionPlan')
    for tier, (members, exports) in DEFAULTS.items():
        SubscriptionPlan.objects.filter(tier=tier).update(
            max_team_members=members, can_export_reports=exports,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0008_seller_ledger_and_plan_limits'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

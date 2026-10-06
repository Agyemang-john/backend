"""
Mark existing active accounts' email addresses as verified.

Before this release an account could only become active by clicking the
emailed activation link (or by signing in with Google), so every active user
has already proven their address. Phones were never verified, so
phone_verified_at is left empty: those customers complete a one-time SMS step
before opening a store.
"""

from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    User = apps.get_model('userauths', 'User')
    User.objects.filter(is_active=True, email_verified_at__isnull=True).update(
        email_verified_at=F('date_joined'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('userauths', '0006_contact_verification_otp_purpose'),
    ]

    operations = [
        # Reverse is a no-op: the column itself is dropped when 0006 is reversed.
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

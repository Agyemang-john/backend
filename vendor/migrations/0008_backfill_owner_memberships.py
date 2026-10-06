"""
Move store access from User.role to team membership.

1. Every existing store gets an active 'owner' VendorMember for Vendor.user,
   which is what new stores get from the post_save signal.
2. Users with role='vendor' go back to 'customer'. Access now comes from
   membership + Vendor.is_approved, so the old role value is meaningless,
   and leaving it in place would only mislead anyone reading the admin.

Reversible: going backwards restores role='vendor' for owners of approved
stores, which is exactly what the old Vendor.save() kept in sync.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Vendor = apps.get_model('vendor', 'Vendor')
    VendorMember = apps.get_model('vendor', 'VendorMember')
    User = apps.get_model('userauths', 'User')

    existing = set(VendorMember.objects.values_list('vendor_id', flat=True))
    VendorMember.objects.bulk_create(
        [
            VendorMember(vendor_id=vendor_id, user_id=user_id, role='owner',
                         is_active=True, added_by_id=user_id)
            for vendor_id, user_id in Vendor.objects.values_list('id', 'user_id').iterator()
            if vendor_id not in existing
        ],
        batch_size=1000,
    )
    User.objects.filter(role='vendor').update(role='customer')


def backwards(apps, schema_editor):
    Vendor = apps.get_model('vendor', 'Vendor')
    User = apps.get_model('userauths', 'User')
    owner_ids = Vendor.objects.filter(is_approved=True).values_list('user_id', flat=True)
    User.objects.filter(pk__in=list(owner_ids)).update(role='vendor')


class Migration(migrations.Migration):

    dependencies = [
        ('vendor', '0007_vendor_team'),
        ('userauths', '0007_backfill_email_verified_at'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

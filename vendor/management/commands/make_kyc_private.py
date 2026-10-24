"""
Move identity documents uploaded before vendor/kyc.py into private storage.

Each old file is read from the public media area, checked like a new upload,
saved privately under a random name, and the public copy is deleted. Files
already under kyc/ are skipped, so the command is safe to run again.

    python manage.py make_kyc_private            # do it
    python manage.py make_kyc_private --dry-run  # list what would move
"""

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from vendor.kyc import KycFilesMixin, clean_kyc_upload
from vendor.models import Vendor


class Command(BaseCommand):
    help = "Move seller identity documents out of public storage."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, dry_run=False, **options):
        moved = skipped = failed = 0
        for vendor in Vendor.objects.all().iterator():
            for field_name in KycFilesMixin.KYC_FIELDS:
                field = getattr(vendor, field_name)
                old_name = field.name
                if not old_name or old_name.startswith('kyc/'):
                    continue
                if dry_run:
                    self.stdout.write(f"would move vendor={vendor.pk} {field_name}: {old_name}")
                    moved += 1
                    continue
                try:
                    with default_storage.open(old_name, 'rb') as fh:
                        content = ContentFile(fh.read(), name=old_name.rsplit('/', 1)[-1])
                except (OSError, ValueError) as exc:
                    self.stderr.write(f"missing vendor={vendor.pk} {field_name} {old_name}: {exc}")
                    failed += 1
                    continue
                try:
                    clean_kyc_upload(content)
                except ValidationError:
                    # Not a document we'd accept today: keep it out of public
                    # view anyway, under a neutral extension.
                    content.name = 'document.bin'
                    skipped += 1
                field.save(content.name, content, save=False)
                Vendor.objects.filter(pk=vendor.pk).update(**{field_name: field.name})
                default_storage.delete(old_name)
                moved += 1
        self.stdout.write(self.style.SUCCESS(
            f"{'Would move' if dry_run else 'Moved'} {moved} document(s); "
            f"{skipped} had an unexpected type; {failed} could not be read."))

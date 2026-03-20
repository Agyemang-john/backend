# payments/management/commands/seed_email_templates.py
#
# Usage:
#   python manage.py seed_email_templates
#   python manage.py seed_email_templates --reset   (overwrites existing records)

from django.core.management.base import BaseCommand
from payments.email_models import EmailTemplate


TEMPLATES = [
    (
        'confirmation',
        'Your {{ plan_name }} Plan is Active — Negromart',
        'subscriptions/subscription_confirmation.html',
    ),
    (
        'renewal_success',
        'Subscription Renewed — Negromart',
        'subscriptions/renewal_success.html',
    ),
    (
        'expiring_soon',
        'Your subscription expires in {{ days_left }} days — Negromart',
        'subscriptions/expiring_soon.html',
    ),
    (
        'expired',
        'Your subscription has expired — Negromart',
        'subscriptions/subscription_expired.html',
    ),
    (
        'payment_failed',
        'Action required: Update your payment method — Negromart',
        'subscriptions/payment_failed.html',
    ),
    (
        'cancellation',
        'Subscription cancelled — Negromart',
        'subscriptions/subscription_cancelled.html',
    ),
]


class Command(BaseCommand):
    help = 'Seed the EmailTemplate table with default Negromart subscription email templates.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Overwrite subject and html_file on existing records.',
        )

    def handle(self, *args, **options):
        reset = options['reset']
        created_count = 0
        updated_count = 0
        skipped_count = 0

        for type_, subject, html_file in TEMPLATES:
            defaults = {'subject': subject, 'html_file': html_file, 'is_active': True}

            if reset:
                obj, created = EmailTemplate.objects.update_or_create(
                    type=type_, defaults=defaults
                )
                if created:
                    created_count += 1
                    self.stdout.write(self.style.SUCCESS(f'  [created]  {type_}'))
                else:
                    updated_count += 1
                    self.stdout.write(self.style.WARNING(f'  [updated]  {type_}'))
            else:
                obj, created = EmailTemplate.objects.get_or_create(
                    type=type_, defaults=defaults
                )
                if created:
                    created_count += 1
                    self.stdout.write(self.style.SUCCESS(f'  [created]  {type_}'))
                else:
                    skipped_count += 1
                    self.stdout.write(f'  [exists]   {type_}')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. Created: {created_count}  '
            f'Updated: {updated_count}  '
            f'Skipped: {skipped_count}'
        ))

        if skipped_count and not reset:
            self.stdout.write(
                self.style.NOTICE('  Tip: run with --reset to overwrite existing templates.')
            )
from django.core.management.base import BaseCommand

from product.curation import run


class Command(BaseCommand):
    help = 'Curate auto collections and occasion cards now (normally runs nightly via Celery beat).'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Reshuffle everything, not just what is due')
        parser.add_argument('--occasion', help='Only this occasion slug')

    def handle(self, *args, **options):
        summary = run(force=options['force'], occasion_slug=options['occasion'])
        self.stdout.write(self.style.SUCCESS(f'Curated: {summary}'))

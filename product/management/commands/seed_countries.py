from django.core.management.base import BaseCommand
from product.shipping import seed_countries  # Adjust import path

class Command(BaseCommand):
    help = 'Seed the Country model with data from pycountry'

    def handle(self, *args, **kwargs):
        seed_countries()
        self.stdout.write(self.style.SUCCESS('Successfully seeded countries'))
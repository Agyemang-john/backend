# address/tasks.py
from celery import shared_task
import logging
import pycountry
from address.models import Country

logger = logging.getLogger(__name__)

@shared_task
def seed_countries():
    """
    Seed the Country model with all countries from pycountry.
    """
    seeded_count = 0
    for country in pycountry.countries:
        obj, created = Country.objects.get_or_create(
            name=country.name,
            defaults={'name': country.name}
        )
        if created:
            seeded_count += 1
            logger.info(f"Seeded country: {country.name}")
    logger.info(f"Country model seeded with {seeded_count} new entries from pycountry data")
    return {
        'status': 'success',
        'message': f"Seeded {seeded_count} countries. Total countries: {Country.objects.count()}"
    }
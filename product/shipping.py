"""
product/shipping.py
Seeds the Country table. Whether a product ships to a shopper is decided by
the storefront from the product's available_in_regions and the shopper's
delivery-country cookie (address/delivery.py); checkout re-checks it against
the real address (Cart.prevent_checkout_unavailable_products).
"""

import logging

import pycountry
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from address.models import Country

logger = logging.getLogger(__name__)

def seed_countries():
    """
    Seed the Country model with all countries from pycountry.
    Populates name (official or common name) and code (ISO 3166-1 alpha-2).
    Run this in a migration or management command.
    """
    added = 0
    skipped = 0
    errors = 0

    for country in pycountry.countries:
        try:
            # Use common name if available, otherwise official name
            country_name = getattr(country, 'common_name', country.name)
            # Truncate name to fit max_length=40
            if len(country_name) > 40:
                logger.warning(f"Truncating country name '{country_name}' to fit max_length=40")
                country_name = country_name[:40]
            
            # Use alpha_2 code (2 characters, e.g., 'US', 'GH')
            country_code = country.alpha_2

            # Create or update the country
            country_obj, created = Country.objects.get_or_create(
                code=country_code,  # Use code as the primary lookup field
                defaults={
                    'name': country_name,
                }
            )
            
            if created:
                added += 1
                logger.debug(f"Added country: {country_name} ({country_code})")
            else:
                # Update name if it differs (e.g., if previously truncated or changed)
                if country_obj.name != country_name:
                    country_obj.name = country_name
                    country_obj.save()
                    logger.debug(f"Updated country name: {country_name} ({country_code})")
                else:
                    skipped += 1
                    logger.debug(f"Skipped existing country: {country_name} ({country_code})")

        except IntegrityError as e:
            logger.error(f"Failed to add country {country_name} ({country_code}): {str(e)}")
            errors += 1
        except ValidationError as e:
            logger.error(f"Validation error for country {country_name} ({country_code}): {str(e)}")
            errors += 1

    logger.info(f"Country seeding complete: {added} added, {skipped} skipped, {errors} errors")

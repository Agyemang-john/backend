import requests
import logging
import json
from django.core.cache import cache
from address.models import Country, Address
import pycountry
from django.db.models import Q
from django.db import IntegrityError
from django.core.exceptions import ValidationError
# Configure logging
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

def get_user_country_region(request):
    """
    Returns (country, region_name) from user's profile address or geolocation (cached).
    - country: Country object (authenticated users with valid address) or string name (geolocation).
    - region_name: String or None (for logging/analytics, not used in shipping).
    Cache key is based on user ID or IP for anonymous users.
    """
    # Generate cache key
    cache_key = f"location:{request.user.id}" if request.user.is_authenticated else f"location:ip:{request.META.get('REMOTE_ADDR', 'unknown')}"
    cached_location = cache.get(cache_key)
    
    if cached_location:
        return cached_location

    location = (None, None)  # Default

    # Check authenticated user's address first
    if request.user.is_authenticated:
        address = Address.objects.filter(user=request.user, status=True).select_related('user').first()
        if address and address.country:
            location = (address.country, address.region or None)  # Ensure region is None if empty
            cache.set(cache_key, location, timeout=12*60*60)  # Cache for 12 hours
            return location

    # Fallback to IP geolocation API (ip-api.com) for anonymous users or authenticated users without valid address
    try:
        response = requests.get(f'http://ip-api.com/json', timeout=5)
        response.raise_for_status()  # Raise for bad status codes
        data = response.json()
        
        if data.get('status') == 'success':
            country_code = data.get('countryCode')
            if country_code:
                # Map country code to name using pycountry
                country_obj = pycountry.countries.get(alpha_2=country_code)
                country_name = country_obj.name if country_obj else data.get('country', 'Unknown')
                region_name = data.get('regionName') or None
                location = (country_name, region_name)
                cache.set(cache_key, location, timeout=12*60*60)  # Cache for 12 hours
                return location
            else:
                logger.warning(f"Geolocation API missing countryCode: {data}")
        else:
            logger.warning(f"Geolocation API returned non-success status: {data}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Geolocation API request failed for IP: {str(e)}")


    return location

def can_product_ship_to_user(request, product):
    """
    Checks if a product can ship to the user's country (by address or IP).
    Assumes available_in_regions is ManyToMany to Country.
    Returns (can_ship, location_info) where location_info is country name.
    """
    country_result, region_name = get_user_country_region(request)

    if not country_result:
        user_info = f"user:{request.user.id}" if request.user.is_authenticated else f"ip:{request.META.get('REMOTE_ADDR', 'unknown')}"
        logger.warning(f"No country identified for shipping check for {user_info}")
        return False, None

    # Resolve to Country object (if string from geolocation, look it up)
    country = None
    country_name = None
    if isinstance(country_result, str):  # From geolocation
        country_name = country_result
        cache_key = f"country:{country_name.lower().replace(' ', '_')}"
        country = cache.get(cache_key)
        if not country:
            try:
                # Try matching by name or official name to handle variations
                country = Country.objects.get(
                    Q(name__iexact=country_name)
                )
                cache.set(cache_key, country, timeout=24*60*60)  # Cache for 24 hours
            except Country.DoesNotExist:
                logger.error(f"Country not found in database: {country_name}")
                return False, country_name
            except Country.MultipleObjectsReturned:
                logger.error(f"Multiple countries found for: {country_name}")
                return False, country_name
    else:  # Already a Country object from address
        country = country_result
        country_name = country

    # Check if product ships to the country
    if not product.available_in_regions.exists():
        # Log warning for potential misconfiguration
        logger.warning(f"No shipping regions specified for product {product.id}; assuming global shipping")
        if region_name:
            logger.info(f"Allowing shipping to {country_name}, region: {region_name}")
        return True, country_name
    elif product.available_in_regions.filter(id=country.id).exists():
        if region_name:
            logger.info(f"Shipping allowed to {country_name}, region: {region_name}")
        return True, country_name
    else:
        logger.warning(f"Product {product.id} does not ship to {country_name}")
        return False, country_name
import requests
import logging
import json
from django.core.cache import cache
from address.models import Country, Address
import pycountry

# Configure logging
logger = logging.getLogger(__name__)

def seed_countries():
    """
    Seed the Country model with all countries from pycountry.
    Run this in a migration or management command.
    """
    for country in pycountry.countries:
        Country.objects.get_or_create(
            name=country.name,
            defaults={'name': country.name}  # Ensure unique names
        )
    logger.info("Country model seeded with pycountry data")

def get_user_country_region(request):
    """
    Returns (country, region_name) from user's profile address or geolocation (cached).
    - country: Country object (authenticated users) or string name (geolocation).
    - region_name: String or None (for logging/analytics, not used in shipping).
    Cache key is based on user ID or IP for anonymous users.
    """
    cache_key = f"location:{request.user.id}" if request.user.is_authenticated else f"location:ip:{request.META.get('REMOTE_ADDR', 'unknown')}"
    cached_location = cache.get(cache_key)
    
    if cached_location:
        return cached_location

    location = None, None  # Default

    # Check authenticated user's address first
    if request.user.is_authenticated:
        address = Address.objects.filter(user=request.user, status=True).select_related('user').first()
        if address:
            location = (address.country, address.region)  # country is Country object
            cache.set(cache_key, location, timeout=12*60*60)  # Cache for 12 hours
            return location

    # Fallback to free IP geolocation API (ip-api.com)
    try:
        response = requests.get('http://ip-api.com/json/', timeout=5)
        response.raise_for_status()  # Raise an error for bad status codes
        data = response.json()
        
        if data['status'] == 'success':
            country_code = data['countryCode']
            # Use pycountry to map country code to name
            country_obj = pycountry.countries.get(alpha_2=country_code)
            country_name = country_obj.name if country_obj else data['country']
            region_name = data.get('regionName') or None
            location = (country_name, region_name)  # country is string for geolocation
            cache.set(cache_key, location, timeout=12*60*60)  # Cache for 12 hours
            return location
        else:
            logger.warning(f"Geolocation API returned non-success status: {data}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Geolocation API request failed: {str(e)}")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse geolocation response: {str(e)}")
    except KeyError:
        logger.error("Unexpected response format from geolocation API")

    return location

def can_product_ship_to_user(request, product):
    """
    Checks if a product can ship to the user's country (by address or IP).
    Assumes available_in_regions is ManyToMany to Country.
    Returns (can_ship, location_info) where location_info is country name.
    """
    country_result, region_name = get_user_country_region(request)

    if not country_result:
        logger.warning("No country identified for shipping check")
        return False, None

    # Resolve to Country object (if string from geolocation, look it up)
    if isinstance(country_result, str):  # From geolocation
        country_name = country_result
        cache_key = f"country:{country_name.lower()}"
        country = cache.get(cache_key)
        if not country:
            try:
                country = Country.objects.get(name__iexact=country_name)
                cache.set(cache_key, country, timeout=24*60*60)  # Cache for 24 hours
            except Country.DoesNotExist:
                logger.error(f"Country not found: {country_name}")
                return False, country_name
    else:  # Already a Country object from address
        country = country_result
        country_name = country.name

    # Check if product ships to the country
    if product.available_in_regions.filter(id=country.id).exists():
        # Log region for analytics, but don't block shipping
        if region_name:
            logger.info(f"Shipping allowed to {country_name}, region: {region_name}")
        return True, country_name
    elif not product.available_in_regions.exists():
        # If no countries specified, assume shipping is allowed globally
        if region_name:
            logger.info(f"No shipping countries specified; allowing to {country_name}, region: {region_name}")
        return True, country_name
    else:
        logger.warning(f"Product does not ship to {country_name}")
        return False, country_name
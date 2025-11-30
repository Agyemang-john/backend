import requests
import logging
from ipware import get_client_ip as ipware_get_client_ip
from django.core.cache import cache
from address.models import Country, Address
import pycountry
from django.db.models import Q
from django.db import IntegrityError
from django.core.exceptions import ValidationError

from django.conf import settings
import ipaddress    
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

def is_valid_ip(ip):
    """Validate if the given string is a valid IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False

def get_ip_address_from_request(request):
    # Fallback: X-Real-IP or REMOTE_ADDR
    ip, _ = ipware_get_client_ip(request)  # Use ipware properly
    if ip is None:
        ip = request.META.get('REMOTE_ADDR', '127.0.0.1')
    return ip

def get_user_country_region(request):
    """Determine the user's country and region based on authentication or IP."""
    # Determine cache key
    if request.user.is_authenticated:
        cache_key = f"location:user:{request.user.id}"
    else:
        ip = get_ip_address_from_request(request)
        cache_key = f"location:ip:{ip}"

    # Check cache first
    cached_location = cache.get(cache_key)
    if cached_location:
        logger.debug(f"Cache hit for {cache_key}: {cached_location}")
        return cached_location

    country = None
    region_name = None

    # Check address for authenticated user
    if request.user.is_authenticated:
        address = Address.objects.filter(user=request.user, status=True).first()
        if address and address.country:
            try:
                country_obj = Country.objects.get(
                    Q(name__iexact=address.country.strip()) | Q(code__iexact=address.country.strip())
                )
                country = country_obj
                region_name = address.region.strip() if address.region else None
                location = (country, region_name)
                cache.set(cache_key, location, 12 * 60 * 60)
                logger.debug(f"Location from address for user {request.user.id}: {location}")
                return location
            except Country.DoesNotExist:
                logger.warning(f"Address country not found: {address.country}")

    # Fallback to IP-based geolocation for unauthenticated users
    ip = get_ip_address_from_request(request)
    if ip.startswith(('10.', '172.', '192.', '127.')):
        logger.warning(f"Skipping geolocation for private IP: {ip}")
        if settings.DEBUG:
            location = ('Ghana', None)  # Default for development
            cache.set(cache_key, location, 12 * 60 * 60)
            logger.debug(f"Using default location for private IP in DEBUG mode: {location}")
            return location
        location = (None, None)
        cache.set(cache_key, location, 12 * 60 * 60)
        return location

    try:
        response = requests.get(f"http://ip-api.com/json/{ip}", timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "success":
            country_code = data.get("countryCode")
            country_name = data.get("country", "Unknown")
            region_name = data.get("regionName")

            if country_code:
                country_obj = pycountry.countries.get(alpha_2=country_code)
                country_name = country_obj.name if country_obj else country_name

            location = (country_name, region_name)
            cache.set(cache_key, location, 12 * 60 * 60)
            logger.debug(f"Geolocation for IP {ip}: {location}")
            return location
        else:
            logger.warning(f"Geolocation API failed for IP {ip}: {data.get('message')}")
    except requests.RequestException as e:
        logger.error(f"Geolocation lookup failed for IP {ip}: {str(e)}")

    # Cache the failure to avoid repeated lookups
    location = (None, None)
    cache.set(cache_key, location, 12 * 60 * 60)
    return location

def can_product_ship_to_user(request, product):
    """
    Checks if product ships to user's country.
    - Returns (True/False, country_name)
    """
    country_result, region_name = get_user_country_region(request)
    logger.debug(f"Country result: {country_result}, Region: {region_name}")

    if not country_result:
        user_info = (
            f"user:{request.user.id}"
            if request.user.is_authenticated
            else f"ip:{get_ip_address_from_request(request)}"
        )
        logger.warning(f"No country identified for shipping check for {user_info}")
        return False, None

    # Resolve to Country object (if geolocation returned string)
    if isinstance(country_result, str):
        country_name = country_result.strip()
        cache_key = f"country_obj:{country_name.lower().replace(' ', '_')}"
        country = cache.get(cache_key)
        if not country:
            try:
                country = Country.objects.get(Q(name__iexact=country_name) | Q(code__iexact=country_name))
                cache.set(cache_key, country, 24 * 60 * 60)
            except Country.DoesNotExist:
                logger.warning(f"Country not found in DB: {country_name}")
                return False, country_name
            except Country.MultipleObjectsReturned:
                logger.error(f"Multiple countries found for: {country_name}")
                return False, country_name
    else:
        country = country_result
        country_name = country.name if country else None

    # Check shipping eligibility
    if not country:
        logger.warning(f"No valid country object for shipping check")
        return False, country_name

    if not product.available_in_regions.exists():
        logger.info(f"Product {product.id} has no regions; assuming global shipping.")
        return True, country_name

    if product.available_in_regions.filter(id=country.id).exists():
        logger.info(f"Product {product.id} ships to {country_name}.")
        return True, country_name

    logger.info(f"Product {product.id} does NOT ship to {country_name}.")
    return False, country_name
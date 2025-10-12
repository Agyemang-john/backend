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

def get_client_ip(request):
    """Get client IP with Digital Ocean/Nginx support"""
    # Digital Ocean load balancer headers
    do_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if do_forwarded:
        ip = do_forwarded.split(',')[0].strip()
        if ip:
            return ip
    
    # Standard headers
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
        if ip and ip.lower() != 'unknown':
            return ip
    
    # Fallback headers
    real_ip = request.META.get('HTTP_X_REAL_IP')
    if real_ip:
        return real_ip
    
    return request.META.get('REMOTE_ADDR', 'unknown')

def get_user_country_region(request):
    if request.user.is_authenticated:
        cache_key = f"location:user:{request.user.id}"
    else:
        ip = get_client_ip(request)
        cache_key = f"location:ip:{ip}"

    cached_location = cache.get(cache_key)
    if cached_location:
        return cached_location

    country = None
    region_name = None

    # Check address for authenticated user
    if request.user.is_authenticated:
        address = Address.objects.filter(user=request.user, status=True).first()
        if address and address.country:
            try:
                country_obj = Country.objects.get(
                    Q(name__iexact=address.country.strip()) | Q(code__iexact=address.country.strip()) | Q(name__icontains=address.country.strip())
                )
                country = country_obj
            except Country.DoesNotExist:
                logger.warning(f"Address country not found: {address.country}")
            region_name = address.region.strip() if address.region else None
            location = (country, region_name)
            cache.set(cache_key, location, 12 * 60 * 60)
            return location

    # Fallback to IP-based geolocation
    ip = get_client_ip(request)
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}", timeout=5)
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
            return location
    except requests.RequestException as e:
        logger.error(f"Geolocation lookup failed: {str(e)}")

    return (None, None)


def can_product_ship_to_user(request, product):
    """
    Checks if product ships to user's country.
    - Returns (True/False, country_name)
    """
    country_result, region_name = get_user_country_region(request)
    if not country_result:
        user_info = (
            f"user:{request.user.id}"
            if request.user.is_authenticated
            else f"ip:{request.META.get('REMOTE_ADDR', 'unknown')}"
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
                country = Country.objects.filter(
                    Q(name__iexact=country_name) | 
                    Q(code__iexact=country_name) |
                    Q(name__icontains=country_name)
                ).first()
                cache.set(cache_key, country, 24 * 60 * 60)
            except Country.DoesNotExist:
                logger.warning(f"Country not found in DB: {country_name}")
                return False, country_name
            except Country.MultipleObjectsReturned:
                logger.error(f"Multiple countries found for: {country_name}")
                return False, country_name
    else:
        country = country_result
        country_name = country.name

    # 3️⃣ Check shipping eligibility
    if not product.available_in_regions.exists():
        logger.info(f"Product {product.id} has no regions; assuming global shipping.")
        return True, country_name

    if product.available_in_regions.filter(id=country.id).exists():
        logger.info(f"Product {product.id} ships to {country_name}.")
        return True, country_name

    logger.info(f"Product {product.id} does NOT ship to {country_name}.")
    return False, country_name
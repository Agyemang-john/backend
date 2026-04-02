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
    """
    Extract the real client IP from the request, handling:
    - Next.js SSR on Vercel (sends X-Client-IP with the user's real IP)
    - Cloudflare CDN (sends CF-Connecting-IP)
    - Docker + Nginx (sends X-Forwarded-For / X-Real-IP)

    Resolution order:
    1. X-Client-IP        — set by Next.js SSR to forward the user's real IP
    2. CF-Connecting-IP   — set by Cloudflare, most trustworthy if using CF
    3. django-ipware      — parses X-Forwarded-For with trusted-proxy awareness
    4. X-Real-IP          — set by Nginx (proxy_set_header X-Real-IP $remote_addr)
    5. REMOTE_ADDR        — last resort (will be Docker/Vercel IP in hosted setups)
    """
    # 1. Next.js SSR: the frontend server forwards the user's real IP via X-Client-IP
    #    because SSR requests come from Vercel's US servers, not the user's browser.
    client_ip = request.META.get('HTTP_X_CLIENT_IP')
    if client_ip and is_valid_ip(client_ip):
        return client_ip

    # 2. Cloudflare provides the true client IP in a dedicated header
    cf_ip = request.META.get('HTTP_CF_CONNECTING_IP')
    if cf_ip and is_valid_ip(cf_ip):
        return cf_ip

    # 3. Let django-ipware parse X-Forwarded-For using settings
    #    proxy_count=0 → best-effort (picks leftmost public IP)
    ip, is_routable = ipware_get_client_ip(request)

    if ip and is_routable:
        return str(ip)

    # 4. Fallback: Nginx sets X-Real-IP to $remote_addr
    x_real_ip = request.META.get('HTTP_X_REAL_IP')
    if x_real_ip and is_valid_ip(x_real_ip):
        return x_real_ip

    # 5. Last resort
    return request.META.get('REMOTE_ADDR', '127.0.0.1')

def get_user_country_region(request):
    if request.user.is_authenticated:
        cache_key = f"location:user:{request.user.id}"
    else:
        ip = get_ip_address_from_request(request)
        cache_key = f"location:ip:{ip}"

    cached_location = cache.get(cache_key)
    if cached_location:
        return cached_location

    country = None
    region_name = None

    if request.user.is_authenticated:
        address = Address.objects.filter(user=request.user, status=True).first()
        if address and address.country:
            try:
                country_obj = Country.objects.get(
                    Q(name__iexact=address.country.strip()) | Q(code__iexact=address.country.strip())
                )
                location = (country_obj, address.region.strip() if address.region else None)
                cache.set(cache_key, location, 12 * 60 * 60)
                return location
            except Country.DoesNotExist:
                logger.warning(f"Address country not found: {address.country}")

    ip = get_ip_address_from_request(request)
    
    try:
        ip_obj = ipaddress.ip_address(ip)
        is_private = ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
    except ValueError:
        is_private = True

    if is_private:
        logger.warning(f"Skipping geolocation for private/invalid IP: {ip}")
        if settings.DEBUG:
            location = ('Ghana', None)
            cache.set(cache_key, location, 12 * 60 * 60)
            return location
        return (None, None)

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

    location = (None, None)
    cache.set(cache_key, location, 60 * 60)
    return location

    # logger.warning(f"Country result: {country_result}, Region: {region_name}")
        # value = country_result.strip()
    
    
def can_product_ship_to_user(request, product):
    country_result, region_name = get_user_country_region(request)

    logger.warning(f"Country result: {country_result}, Region: {region_name}")


    if not country_result:
        return False, None

    # Normalize country
    if isinstance(country_result, str):
        value = country_result.strip()

        country = Country.objects.filter(
            Q(name__iexact=value) |
            Q(code__iexact=value)
        ).first()

        if not country:
            logger.warning(f"Country not found in DB: {value}")
            return False, value

        country_name = country.name
    else:
        country = country_result
        country_name = country.name

    # Shipping rules
    if not product.available_in_regions.exists():
        return True, country_name

    if product.available_in_regions.filter(id=country.id).exists():
        return True, country_name

    return False, country_name
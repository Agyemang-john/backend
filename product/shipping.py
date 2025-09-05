import geocoder
from django.core.cache import cache
from address.models import Country, Address

COUNTRY_CODE_MAPPING = {
    'GH': 'Ghana',
    'UK': 'United Kingdom',
    'NG': 'Nigeria',
    'US': 'United States',
    # Add more as needed
}

import re

def normalize_region_name(name):
    if not name:
        return ''
    return re.sub(r'\s*region\s*$', '', name, flags=re.IGNORECASE).strip().lower()


def get_user_country_region(request):
    """
    Returns (country_name, region_name) from user's profile address or geolocation (cached).
    """
    if request.auth:
        address = Address.objects.filter(user=request.user, status=True).first()
        if address:
            return address.country, address.region
    
    # Use 'me' for geolocation (will use the request IP)
    
    g = geocoder.ip('me')
    user_region = None
    if g.ok:
        user_region = g.country
        country_name = COUNTRY_CODE_MAPPING.get(user_region, user_region)
        region_name = g.state or None

        # Cache the result for 12 hours (adjust as needed)
        return country_name, region_name
        
    return None, None


def can_product_ship_to_user(request, product):
    """
    Checks if a product can ship to the user's location (by address or IP).
    """
    country_name, region_name = get_user_country_region(request)

    if not country_name:
        return False, None
    try:
        country = Country.objects.get(name__iexact=country_name)
    except Country.DoesNotExist:
        print(f"It does not exist: {country_name}")  
        return False, country_name

    regions = product.available_in_regions.filter(country=country)
    if not regions.exists():
        return False, country_name

    # Check for normalized region name match
    if region_name:
        normalized_input = normalize_region_name(region_name)
        matched = any(
            normalize_region_name(region.name) == normalized_input
            for region in regions
        )
        return matched, region_name
    
    else:
        return True, country_name
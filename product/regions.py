"""
product/regions.py

Which country a storefront request is shopping from, and the product filter
that goes with it. Used by the occasion and collection endpoints so
country-targeted occasions (product.SeasonalEvent / Occasion.countries) reach
the right shoppers and nobody is shown a product that won't ship to them.

Resolution order: the X-Country header (set by the storefront from the
shopper's `country` cookie), then a CDN geo header (Cloudflare CF-IPCountry,
Vercel X-Vercel-IP-Country), then settings.STOREFRONT_DEFAULT_COUNTRY.
"""

import re

from django.conf import settings
from django.db.models import Q

from .models import Product

_CODE = re.compile(r'^[A-Za-z]{2}$')


def storefront_country(request):
    """ISO alpha-2 code, upper-case."""
    for header in ('X-Country', 'CF-IPCountry', 'X-Vercel-IP-Country'):
        value = (request.headers.get(header) or '').strip()
        if _CODE.match(value) and value.upper() != 'XX':  # Cloudflare sends XX when unknown
            return value.upper()
    return getattr(settings, 'STOREFRONT_DEFAULT_COUNTRY', 'GH')


def available_in(queryset, country_code):
    """Products with no region restriction, or restricted to a list that includes `country_code`."""
    through = Product.available_in_regions.through.objects
    return queryset.filter(
        ~Q(id__in=through.values('product_id'))
        | Q(id__in=through.filter(country__code__iexact=country_code).values('product_id'))
    )

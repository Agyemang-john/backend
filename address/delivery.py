"""
address/delivery.py

The signed-in shopper's delivery country, kept in a cookie so the storefront
can tell whether a product ships to them while it renders the page, with no
location lookup per page view.

`ship_country` holds the ISO alpha-2 code of the shopper's default address.
The storefront prefers it over the `country` cookie its proxy sets from Vercel
geo, which covers guests and shoppers without an address:

    ship_country  →  country  →  x-vercel-ip-country  →  unknown

It's refreshed wherever the default address can change, at sign-in and by the
product page's auth-state call (so sessions on other devices catch up), and
cleared at sign-out. Not HttpOnly: the browser reads it too.
"""

from django.conf import settings
from django.core.cache import cache

from .models import Address, Country

COOKIE = 'ship_country'
MAX_AGE = int(settings.AUTH_REFRESH_MAX_AGE)

_CODES_KEY = 'address:country_codes:v1'


def _country_codes():
    """{lower-case name or code: CODE}. A few hundred rows that almost never change."""
    codes = cache.get(_CODES_KEY)
    if codes is None:
        codes = {}
        for name, code in Country.objects.exclude(code__isnull=True).exclude(code='').values_list('name', 'code'):
            codes[name.lower()] = code.upper()
            codes[code.lower()] = code.upper()
        cache.set(_CODES_KEY, codes, 60 * 60 * 24)
    return codes


def country_code(value):
    """ISO code for an Address.country value, which may hold a name or a code."""
    if not value:
        return None
    return _country_codes().get(value.strip().lower())


def default_country(user):
    """ISO code of the user's default address, or None if they have none."""
    value = (Address.objects.filter(user=user, status=True)
             .values_list('country', flat=True).first())
    return country_code(value)


def set_cookie(response, request, code):
    """Point the cookie at `code` (None clears it). Skips the header when nothing changes."""
    if code == request.COOKIES.get(COOKIE):
        return
    if code is None:
        delete_cookie(response)
        return
    response.set_cookie(
        COOKIE, code,
        max_age=MAX_AGE,
        path='/',
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=False,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        domain=settings.AUTH_COOKIE_DOMAIN,
    )


def sync_cookie(response, request, user):
    """Set the cookie from the user's current default address. Returns the code."""
    code = default_country(user)
    set_cookie(response, request, code)
    return code


def delete_cookie(response):
    response.delete_cookie(COOKIE, path='/', domain=settings.AUTH_COOKIE_DOMAIN)

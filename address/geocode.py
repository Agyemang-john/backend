"""
Kept for anything importing the old helper; geocoding lives in address/geo.py
(server-side, cached, provider-agnostic).
"""

from .geo import geocode


def geocode_address(address_str, country_code=None):
    """(lat, lon) for a free-text address, or (None, None)."""
    found = geocode(address_str, country_code=country_code)
    return (found[0], found[1]) if found else (None, None)

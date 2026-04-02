"""
Geocoding utility for the address app.

Provides a helper function that resolves a free-text address string
to latitude / longitude coordinates using the OpenStreetMap Nominatim
API.  Results are returned as a (lat, lon) tuple, or (None, None) on
failure.
"""

import logging
import requests

logger = logging.getLogger(__name__)


def geocode_address(address_str):
    """
    Geocode an address string using OpenStreetMap's Nominatim API.
    Returns (lat, lon) or (None, None) if not found.
    """
    if not address_str:
        return None, None

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": address_str,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    }
    headers = {"User-Agent": "Negromart (support@negromart.com)"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        data = response.json()
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
    except Exception as exc:
        # Log the error instead of silently swallowing it
        logger.error("Geocoding failed for '%s': %s", address_str, exc)

    return None, None

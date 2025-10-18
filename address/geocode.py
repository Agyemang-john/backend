# utils/geocode.py
import requests

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
    except Exception:
        pass

    return None, None

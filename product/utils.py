

from django.contrib.gis.geoip2 import GeoIP2


def get_region_with_geoip(ip):
    try:
        geo = GeoIP2()
        return geo.city(ip)['region']  # You can use 'country_name' or 'city' too
    except Exception as e:
        print("GeoIP2 error:", e)
        return None


def calculate_packaging_fee(weight, volume):
    # Example rates, adjust as needed
    weight_rate = 1.0  # Packaging fee per kg
    volume_rate = 1.0  # Packaging fee per cubic meter

    weight_fee = weight * weight_rate
    volume_fee = volume * volume_rate

    # Choose the higher fee or sum both if needed
    # packaging_fee = max(weight_fee, volume_fee)
    packaging_fee = weight_fee + volume_fee
    return packaging_fee



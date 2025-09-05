# services.py

# import geopy.distance
import math
from django.apps import apps

def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in kilometers
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon1 - lon2)
    a = (math.sin(d_lat / 2) ** 2 + 
         math.cos(math.radians(lat1)) * 
         math.cos(math.radians(lat2)) * 
         math.sin(d_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance = R * c
    return distance

def calculate_delivery_fee(vendor_lat, vendor_lon, buyer_lat, buyer_lon, delivery_option):
    DeliveryRate = apps.get_model('order', 'DeliveryRate')  # Use the correct app label
    delivery_fee = 0
    
    distance = haversine(vendor_lat, vendor_lon, buyer_lat, buyer_lon)
    rate_record = DeliveryRate.objects.first()
    if not rate_record:
        raise ValueError("Delivery rate not set in the database")

    base_price = rate_record.base_price  # Base price for up to 5 kilometers
    rate_per_km = rate_record.rate_per_km # Additional cost per kilometer

    if distance <= 5:
        return float(base_price) + float(delivery_option)
    else:
        extra_distance = float((distance) - float(5))
        delivery_fee = float(base_price) + (float(extra_distance) * float(rate_per_km)) + float(delivery_option)
        return delivery_fee

    # rate_per_km = float(rate_record.rate_per_km)
    # delivery_fee = float(distance * rate_per_km) + float(delivery_option)
    # return delivery_fee




from django.contrib.gis.geoip2 import GeoIP2
from django.db.models import Case, When

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

def get_recently_viewed_products(request, limit=10):
    from product.models import Product
    raw_ids = request.session.get('recently_viewed', [])[:limit]
    if not raw_ids:
        return Product.objects.none()

    # Safely convert to integers
    ids = []
    for pid in raw_ids:
        try:
            ids.append(int(pid))
        except (ValueError, TypeError):
            continue

    if not ids:
        return Product.objects.none()

    # Preserve exact order from session
    preserved_order = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(ids)])

    return Product.published.filter(pk__in=ids).order_by(preserved_order)


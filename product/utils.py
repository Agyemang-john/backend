

from django.contrib.gis.geoip2 import GeoIP2
from django.db.models import Case, When
from functools import lru_cache

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

@lru_cache(maxsize=128)
def _preserve_order(ids):
    """Cache the Case/When objects – huge speedup!"""
    return Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(ids)])

def get_recently_viewed_products(request, limit=10):
    from product.models import Product
    raw_ids = request.session.get('recently_viewed', [])[:limit]
    if not raw_ids:
        return Product.objects.none()

    # Convert once and safely
    try:
        ids = [int(pid) for pid in raw_ids if str(pid).isdigit()]
    except (ValueError, TypeError):
        ids = []

    if not ids:
        return Product.objects.none()

    # This is now cached per unique list of IDs!
    order = _preserve_order(tuple(ids))  # tuple for hashability

    return Product.published.filter(pk__in=ids).order_by(order)

def update_recently_viewed(session, product_id, limit=10):
    pid = str(product_id)
    viewed = session.get('recently_viewed', [])
    
    if pid in viewed:
        viewed.remove(pid)        # O(n) but n ≤ 10 → negligible
    viewed.insert(0, pid)
    
    session['recently_viewed'] = viewed[:limit]
    session.modified = True


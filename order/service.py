import math
import requests
from django.conf import settings
from django.apps import apps
from decimal import Decimal
from forex_python.converter import CurrencyRates
from pycountry import countries
import logging
from django.utils import timezone
from collections import defaultdict

logger = logging.getLogger(__name__)


class FeeResult:
    """Custom class to hold delivery fee results."""
    def __init__(self, total, dynamic_quotes=None, invalid_items=None):
        self.total = Decimal(total)
        self.dynamic_quotes = dynamic_quotes or {}
        self.invalid_items = invalid_items or []

    def __float__(self):
        return float(self.total)

    def __repr__(self):
        return f"FeeResult(total={self.total}, dynamic_quotes={self.dynamic_quotes}, invalid_items={self.invalid_items})"


def get_continent_from_country(country_code):
    try:
        country = countries.get(alpha_2=country_code)
        return country.subregion or country.continent or 'Unknown'
    except:
        return 'Unknown'


def get_third_party_shipping_quote(provider, from_country, to_country, weight, volume, vendor_lat=None, vendor_lon=None, buyer_lat=None, buyer_lon=None):
    try:
        if provider != 'DHL':
            raise ValueError(f"Unsupported provider: {provider}. Only DHL is supported.")

        payload = {
            "plannedShippingDateAndTime": timezone.now().strftime("%Y-%m-%dT%H:%M:%S GMT+00:00"),
            "unitOfMeasurement": "metric",
            "isCustomsDeclarable": True,
            "monetaryAmount": [{"type": "declaredValue", "value": 100, "currency": "USD"}],
            "requestAllRates": True,
            "accounts": [{"typeCode": "shipper", "number": settings.DHL_ACCOUNT_NUMBER}],
            "shipper": {"postalAddress": {"countryCode": from_country, "postalCode": "00000"}},
            "receiver": {"postalAddress": {"countryCode": to_country, "postalCode": "00000"}},
            "packages": [{
                "weight": weight,
                "dimensions": {
                    "length": (volume ** (1/3)) * 100,
                    "width":  (volume ** (1/3)) * 100,
                    "height": (volume ** (1/3)) * 100
                }
            }]
        }

        headers = {
            'Authorization': f'Bearer {settings.DHL_API_KEY}',
            'Accept': 'application/json'
        }

        response = requests.post(
            'https://api-c.dhl.com/parcel/de/v2/rating',
            json=payload,
            headers=headers
        )
        response.raise_for_status()
        data = response.json()

        rate = data['products'][0] if data.get('products') else None
        if not rate:
            raise ValueError("No shipping rates returned by DHL")

        cost_currency = rate['totalPrice'][0]['priceCurrency']
        cost = float(rate['totalPrice'][0]['price'])
        min_days = rate.get('estimatedDeliveryDate', {}).get('minDays', 5)
        max_days = rate.get('estimatedDeliveryDate', {}).get('maxDays', 10)

        cr = CurrencyRates()
        cost_ghs = Decimal(str(cr.convert(cost_currency, 'GHS', cost)))

        return cost_ghs, min_days, max_days

    except Exception as e:
        logger.warning(f"DHL API failed: {e}. Falling back to estimates.")
        return Decimal('50.00'), 7, 14


def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) *
         math.sin(d_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# NEW: Campus zone lookup
def get_campus_zone(lat, lon):
    """
    Returns the CampusZone the coordinate falls within, or None.
    Queries DB so new campuses can be added via admin without code changes.
    """
    CampusZone = apps.get_model('order', 'CampusZone')
    for zone in CampusZone.objects.all():
        if haversine(lat, lon, zone.center_lat, zone.center_lon) <= zone.radius_km:
            return zone
    return None


def calculate_tiered_fee(distance, base_price, rate_per_km, option_cost):
    """
    Tiered distance-based fee with a hard cap.
    Used ONLY when campus zone check does not apply.
    """
    FEE_CAP = Decimal('80.00')

    TIERS = [
        (5,            Decimal('0.00')),
        (20,           Decimal('0.70')),
        (100,          Decimal('0.40')),
        (float('inf'), Decimal('0.15')),
    ]

    fee = Decimal(str(base_price))

    if distance > 5:
        prev_limit = 5
        for limit, rate in TIERS:
            if distance <= prev_limit:
                break
            chargeable = Decimal(str(min(distance, limit) - prev_limit))
            fee += chargeable * rate
            prev_limit = limit
            if distance <= limit:
                break

    fee = min(fee, FEE_CAP)
    return float(fee + Decimal(str(option_cost or 0)))


def calculate_delivery_fee(vendor_lat, vendor_lon, buyer_lat, buyer_lon, delivery_option, buyer_country=None, from_country=None, weight=None, volume=None, order_total=None):
    """
    Priority order:
      1. Same campus zone → flat campus fee (or free if order_total >= threshold)
      2. Same country     → tiered distance fee
      3. International    → DHL quote
    """
    if buyer_country == from_country:

        # CAMPUS CHECK — runs before any distance logic
        vendor_zone = get_campus_zone(vendor_lat, vendor_lon)
        buyer_zone  = get_campus_zone(buyer_lat,  buyer_lon)

        if vendor_zone and buyer_zone and vendor_zone.id == buyer_zone.id:
            # Both on the same campus
            logger.info(f"Campus zone delivery detected: {vendor_zone.name}. Applying flat fee.")

            if (vendor_zone.free_delivery_threshold and order_total is not None
                    and Decimal(str(order_total)) >= vendor_zone.free_delivery_threshold):
                logger.info(f"Order total {order_total} meets free delivery threshold. Fee = 0.")
                return 0.0

            return float(vendor_zone.flat_fee)
        # ── END CAMPUS CHECK ──

        # Standard local delivery (tiered + capped)
        DeliveryRate = apps.get_model('order', 'DeliveryRate')
        distance = haversine(vendor_lat, vendor_lon, buyer_lat, buyer_lon)
        rate_record = DeliveryRate.objects.first()

        if not rate_record:
            logger.warning("Delivery rate not set in the database. Using default.")
            return float(delivery_option.cost or 0)

        return calculate_tiered_fee(
            distance,
            rate_record.base_price,
            rate_record.rate_per_km,
            delivery_option.cost
        )

    else:
        # International delivery via DHL
        provider = delivery_option.provider
        if not provider or provider != 'DHL':
            logger.warning(f"DHL required for international delivery to {buyer_country}. Using fallback.")
            return float(delivery_option.cost or 50.00)

        cost, _, _ = get_third_party_shipping_quote(
            provider, from_country, buyer_country, weight or 1.0, volume or 1.0
        )
        return float(cost)


class FeeCalculator:
    @staticmethod
    def calculate_delivery_fee(vendor_lat, vendor_lon, user_lat, user_lon, delivery_option, buyer_country=None, vendor_country=None, weight=None, volume=None, order_total=None):
        try:
            return calculate_delivery_fee(
                vendor_lat, vendor_lon, user_lat, user_lon, delivery_option,
                buyer_country=buyer_country, from_country=vendor_country,
                weight=weight, volume=volume,
                order_total=order_total  # ✅ passed through for free delivery threshold
            )
        except Exception as e:
            logger.warning(f"Failed to calculate delivery fee: {str(e)}")
            return float(delivery_option.cost or 0)

    @staticmethod
    def calculate_total_delivery_fee(items, address, item_type='cart', buyer_country_code=None):
        group_items = defaultdict(list)
        total_delivery_fee = Decimal(0)
        packaging_fees = Decimal(0)
        dynamic_quotes = {}
        invalid_items = []

        if not hasattr(address, 'latitude') or not hasattr(address, 'longitude') or address.latitude is None or address.longitude is None:
            logger.warning("No valid coordinates for address. Falling back to zero delivery fee.")
            return FeeResult(total=Decimal(0), dynamic_quotes=dynamic_quotes, invalid_items=invalid_items)

        buyer_country = buyer_country_code or (address.country if hasattr(address, 'country') and address.country else 'GH')

        for item in items:
            product = item.product
            vendor = product.vendor
            delivery_option = item.selected_delivery_option or FeeCalculator.get_default_delivery_option(item.product)
            weight = product.weight
            volume = product.volume

            if not delivery_option:
                logger.warning(f"No delivery option for product: {product.title}. Skipping.")
                invalid_items.append(product.title)
                continue

            vendor_country = vendor.shipping_from_country.name if vendor.shipping_from_country else 'GH'
            is_international = buyer_country != vendor_country

            if is_international and delivery_option.type != 'international':
                international_option = FeeCalculator.get_default_delivery_option(product, type='international')
                if international_option:
                    delivery_option = international_option
                    logger.info(f"Switched to international option for {product.title}")
                else:
                    logger.warning(f"No international option for {product.title}. Skipping.")
                    invalid_items.append(product.title)
                    continue
            elif not is_international and delivery_option.type == 'international':
                logger.warning(f"Product {product.title} cannot use international option for local delivery. Skipping.")
                invalid_items.append(product.title)
                continue

            packaging_fees += FeeCalculator.calculate_packaging_fee(item)
            key = (vendor, delivery_option)
            group_items[key].append(item)

        for key, group in group_items.items():
            vendor, delivery_option = key
            vendor_country = vendor.shipping_from_country.name if vendor.shipping_from_country else 'GH'
            is_international = buyer_country != vendor_country

            total_weight = sum(Decimal(str(item.product.weight or 0)) * item.quantity for item in group) or Decimal('1.0')
            total_volume = sum(Decimal(str(item.product.volume or 0)) * item.quantity for item in group) or Decimal('1.0')

            # Compute group order total for free-delivery threshold check
            group_order_total = sum(
                Decimal(str(item.product.price)) * item.quantity for item in group
            )

            if is_international:
                provider = delivery_option.provider
                if provider and provider == 'DHL':
                    try:
                        quote_cost, quote_min_days, quote_max_days = get_third_party_shipping_quote(
                            provider, vendor_country, buyer_country, float(total_weight), float(total_volume)
                        )
                        quote_key = f"{vendor.id}_{delivery_option.id}"
                        dynamic_quotes[quote_key] = {
                            'cost': quote_cost,
                            'min_days': quote_min_days,
                            'max_days': quote_max_days,
                            'option': delivery_option
                        }
                        delivery_fee = Decimal(str(quote_cost))
                    except Exception as e:
                        logger.warning(f"DHL quote failed for vendor {vendor.id}: {str(e)}. Using fallback.")
                        delivery_fee = Decimal(str(delivery_option.cost or 50.00))
                else:
                    delivery_fee = Decimal(str(delivery_option.cost or 50.00))
            else:
                delivery_fee = Decimal(str(FeeCalculator.calculate_delivery_fee(
                    vendor.about.latitude, vendor.about.longitude,
                    address.latitude, address.longitude,
                    delivery_option,
                    buyer_country=buyer_country,
                    vendor_country=vendor_country,
                    weight=float(total_weight),
                    volume=float(total_volume),
                    order_total=group_order_total  # ✅ passed for campus free-delivery check
                )))

            total_delivery_fee += delivery_fee

        if invalid_items:
            logger.warning(f"Skipped invalid items: {', '.join(invalid_items)}")

        return FeeResult(
            total=total_delivery_fee + packaging_fees,
            dynamic_quotes=dynamic_quotes,
            invalid_items=invalid_items
        )

    @staticmethod
    def get_default_delivery_option(product, type=None):
        from product.models import ProductDeliveryOption
        options = ProductDeliveryOption.objects.filter(product=product, default=True)
        if type:
            options = options.filter(delivery_option__type=type)
        option = options.first()
        return option.delivery_option if option else None

    @staticmethod
    def calculate_packaging_fee(item):
        weight = item.product.weight or 0  # ✅ also fixed the null crash from earlier
        volume = item.product.volume or 0
        return Decimal(str(weight * volume * 0.1)) * item.quantity

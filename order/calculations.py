# from decimal import Decimal
# from django.core.exceptions import ValidationError
# from .service import calculate_delivery_fee, get_third_party_shipping_quote
# from product.models import ProductDeliveryOption
# import logging

# logger = logging.getLogger(__name__)

# # class FeeCalculator:
# #     @staticmethod
# #     def calculate_delivery_fee(vendor_lat, vendor_lon, user_lat, user_lon, option_cost):
# #         """Calculate delivery fee based on vendor and user coordinates."""
# #         try:
# #             return calculate_delivery_fee(vendor_lat, vendor_lon, user_lat, user_lon, option_cost)
# #         except Exception as e:
# #             raise ValidationError(f"Failed to calculate delivery fee: {str(e)}")

# #     @staticmethod
# #     def calculate_total_delivery_fee(items, address, item_type='cart'):
# #         """Calculate total delivery fee for cart or order items."""
# #         processed_vendors = set()
# #         total_delivery_fee = Decimal(0)
# #         packaging_fees = Decimal(0)

# #         if not hasattr(address, 'latitude') or not hasattr(address, 'longitude') or address.latitude is None or address.longitude is None:
# #             logger.warning(f"No valid coordinates for address {address}. Falling back to zero delivery fee.")
# #             return Decimal(0)

# #         for item in items:
# #             product = item.product
# #             vendor = product.vendor
# #             delivery_option = item.selected_delivery_option or FeeCalculator.get_default_delivery_option(item.product)

# #             if not delivery_option:
# #                 raise ValidationError(f"No delivery option for product: {product.title}")

# #             packaging_fees += FeeCalculator.calculate_packaging_fee(item)
# #             if vendor not in processed_vendors:
# #                 delivery_fee = FeeCalculator.calculate_delivery_fee(
# #                     vendor.about.latitude, vendor.about.longitude,
# #                     address.latitude, address.longitude,
# #                     delivery_option.cost
# #                 )
# #                 total_delivery_fee += Decimal(delivery_fee)
# #                 processed_vendors.add(vendor)
# #             else:
# #                 total_delivery_fee += Decimal(delivery_option.cost)

# #         return total_delivery_fee + packaging_fees

# class FeeCalculator:
#     @staticmethod
#     def calculate_delivery_fee(vendor_lat, vendor_lon, user_lat, user_lon, option_cost, buyer_country=None, vendor_country=None, weight=None, volume=None):
#         """Updated: Pass country/weight for international."""
#         try:
#             return calculate_delivery_fee(
#                 vendor_lat, vendor_lon, user_lat, user_lon, option_cost,
#                 buyer_country=buyer_country, from_country=vendor_country, weight=weight, volume=volume
#             )
#         except Exception as e:
#             raise ValidationError(f"Failed to calculate delivery fee: {str(e)}")

#     @staticmethod
#     def calculate_total_delivery_fee(items, address, item_type='cart', buyer_country_code=None):
#         """Updated: Accept buyer_country_code (ISO); detect international per vendor."""
#         processed_vendors = set()
#         total_delivery_fee = Decimal(0)
#         packaging_fees = Decimal(0)
#         dynamic_quotes = {}  # Cache per vendor for date ranges (cost, min_days, max_days)

#         if not hasattr(address, 'latitude') or not hasattr(address, 'longitude') or address.latitude is None or address.longitude is None:
#             logger.warning(f"No valid coordinates for address {address}. Falling back to zero delivery fee.")
#             return Decimal(0)

#         buyer_country = buyer_country_code or (address.country if hasattr(address, 'country') else 'GH')  # Derive from address/profile

#         for item in items:
#             product = item.product
#             vendor = product.vendor
#             delivery_option = item.selected_delivery_option or FeeCalculator.get_default_delivery_option(item.product)
#             weight = product.weight
#             volume = product.volume

#             if not delivery_option:
#                 raise ValidationError(f"No delivery option for product: {product.title}")

#             packaging_fees += FeeCalculator.calculate_packaging_fee(item)

#             vendor_country = vendor.country if vendor.country else 'GH'
#             is_international = buyer_country != vendor_country

#             # Filter/validate option type
#             if is_international and delivery_option.type != delivery_option.INTERNATIONAL:
#                 raise ValidationError(f"Product {product.title} requires international delivery option for {buyer_country}")
#             elif not is_international and delivery_option.type == delivery_option.INTERNATIONAL:
#                 raise ValidationError(f"Product {product.title} cannot use international option for local delivery")

#             if vendor not in processed_vendors:
#                 # Get dynamic quote for international (also stores for date ranges)
#                 if is_international:
#                     provider = delivery_option.provider
#                     if provider:
#                         quote_cost, quote_min_days, quote_max_days = get_third_party_shipping_quote(
#                             provider, vendor_country, buyer_country, weight, volume
#                         )
#                         dynamic_quotes[vendor.id] = {
#                             'cost': quote_cost,
#                             'min_days': quote_min_days,
#                             'max_days': quote_max_days,
#                             'option': delivery_option
#                         }
#                         delivery_fee = float(quote_cost)
#                     else:
#                         delivery_fee = float(delivery_option.cost or 0)  # Fallback to fixed
#                 else:
#                     # Local: Unchanged
#                     delivery_fee = FeeCalculator.calculate_delivery_fee(
#                         vendor.about.latitude, vendor.about.longitude,
#                         address.latitude, address.longitude,
#                         delivery_option.cost,
#                         buyer_country=buyer_country,
#                         vendor_country=vendor_country,
#                         weight=weight,
#                         volume=volume
#                     )

#                 total_delivery_fee += Decimal(str(delivery_fee))
#                 processed_vendors.add(vendor)
#             else:
#                 # For additional items from same vendor: Add base cost or prorated (adjust as needed)
#                 if vendor.id in dynamic_quotes:
#                     total_delivery_fee += dynamic_quotes[vendor.id]['cost'] * 0.1  # e.g., 10% for extras
#                 else:
#                     total_delivery_fee += Decimal(delivery_option.cost or 0)

#         # Return total + packaging; also expose dynamic_quotes for views (e.g., for date ranges)
#         result = total_delivery_fee + packaging_fees
#         result.dynamic_quotes = dynamic_quotes  # Attach for use in views
#         return result

#     @staticmethod
#     def calculate_packaging_fee(item):
#         """Calculate packaging fee for a single item."""
#         return Decimal(item.product.weight * item.product.volume * 0.1) * item.quantity

#     @staticmethod
#     def get_default_delivery_option(product):
#         """Retrieve default delivery option for a product."""
#         option = ProductDeliveryOption.objects.filter(product=product, default=True).first()
#         return option.delivery_option if option else None
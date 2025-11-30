from decimal import Decimal
from django.shortcuts import get_object_or_404
from rest_framework import status
from .cart_utils import get_authenticated_cart_response, get_guest_cart_response, handle_authenticated_cart, handle_guest_cart, remove_from_guest_cart

from address.models import Address
from address.serializers import AddressSerializer
# from order.service import calculate_delivery_fee
from .models import Cart, CartItem, Order
from product.models import Product, Variants, ProductDeliveryOption
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from .serializers import *
from product.utils import *
from django.http import HttpResponse
from reportlab.pdfgen import canvas
import os
from django.conf import settings
from userauths.models import Profile
from order.service import FeeCalculator
from rest_framework.views import APIView
import logging
logger = logging.getLogger(__name__)

class AddToCartView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        product_id = request.data.get("product_id")
        variant_id = request.data.get("variant_id")
        quantity_change = int(request.data.get("quantity", 1))

        if not product_id:
            return Response({"error": "product_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        product = get_object_or_404(Product, id=product_id)
        variant = get_object_or_404(Variants, id=variant_id, product=product) if variant_id else None
        item_key = f"{product.id}_{variant.id if variant else 'none'}"

        # Handle cart
        if request.user.is_authenticated:
            result = handle_authenticated_cart(request.user, product, variant, quantity_change)
            cart_item_id = result.get("cart_item_id")
        else:
            result = handle_guest_cart(request.session, item_key, quantity_change)
            cart_item_id = None

        # Stock check
        stock_quantity = product.get_stock_quantity(variant)
        is_out_of_stock = (result["quantity"] >= stock_quantity > 0) or (stock_quantity <= 0)

        # Total cart items count
        if request.user.is_authenticated:
            total_cart_quantity = Cart.objects.get(user=request.user).total_quantity
        else:
            total_cart_quantity = sum(request.session.get("guest_cart", {}).values())

        return Response({
            "message": result["message"],
            "quantity": result["quantity"],           # ← This is the item's current quantity
            "is_in_cart": result["is_in_cart"],
            "is_out_of_stock": is_out_of_stock,
            "cart_item_id": cart_item_id,
            "total_cart_quantity": total_cart_quantity,
        })

class RemoveFromCartView(APIView):
    permission_classes = [AllowAny]  # Allow guests!

    def post(self, request):
        product_id = request.data.get("product_id")
        variant_id = request.data.get("variant_id")  # Can be None or string

        if not product_id:
            return Response({"error": "product_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            product = get_object_or_404(Product, id=product_id)
            variant = None
            if variant_id:
                variant = get_object_or_404(Variants, id=variant_id, product=product)

            item_key = f"{product.id}_{variant.id if variant else 'none'}"
            removed = False
            total_cart_quantity = 0

            # ——————— Logged-in User: Remove from DB ———————
            if request.user.is_authenticated:
                cart = Cart.objects.get(user=request.user)  # Must exist
                cart_item = get_object_or_404(
                    CartItem,
                    cart=cart,
                    product=product,
                    variant=variant
                )
                cart_item.delete()
                removed = True

                # Recalculate totals
                total_cart_quantity = cart.total_quantity
                cart_items = cart.cart_items.select_related('product', 'variant')
                packaging_fee = sum(item.product.packaging_fee * item.quantity for item in cart_items.all())

            # ——————— Guest User: Remove from Session ———————
            else:
                removed = remove_from_guest_cart(request.session, item_key)
                total_cart_quantity = sum(request.session.get("guest_cart", {}).values())

            if not removed:
                return Response({
                    "success": False,
                    "message": "Item not found in cart"
                }, status=status.HTTP_404_NOT_FOUND)

            # ——————— Common Response ———————
            if request.user.is_authenticated:
                cart_items = CartItem.objects.filter(cart__user=request.user).select_related('product', 'variant')
                packaging_fee = sum(item.product.packaging_fee * item.quantity for item in cart_items)
                total_amount = Cart.objects.get(user=request.user).total_price
                items_count = cart_items.count()
            else:
                packaging_fee = 0  # You can enhance this later if needed
                total_amount = 0
                items_count = len(request.session.get("guest_cart", {}))

            return Response({
                "success": True,
                "message": "Item removed from cart",
                "total_cart_quantity": total_cart_quantity,
                "cart": {
                    "items_count": items_count,
                    "total_amount": float(total_amount),
                    "packaging_fee": float(packaging_fee),
                }
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({
                "success": False,
                "error": "Something went wrong",
                "detail": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


##############################################################################################
#######################################     CART QUANTITY ####################################
##############################################################################################

class CartQuantityView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            if request.user.is_authenticated:
                try:
                    cart = Cart.objects.get_for_request(request)
                    total_quantity = cart.total_quantity if cart else 0
                except Cart.DoesNotExist:
                    total_quantity = 0
            else:
                # Guest: Use Redis session (exactly like AddToCartView)
                guest_cart = request.session.get("guest_cart", {})
                total_quantity = sum(guest_cart.values())  # Sum of all item quantities

            return Response({
                "quantity": total_quantity
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"CartQuantityView error: {str(e)}", exc_info=True)
            return Response({
                "detail": "Error retrieving cart quantity"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
##############################################################################################
#######################################     CART QUANTITY ####################################
##############################################################################################

class SyncGuestCartView(APIView):
    """
    Called automatically after login.
    Merges the guest session cart (Redis) into the user's database cart.
    No headers, no cookies, no frontend work needed.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Get guest cart from Redis session
        guest_cart = request.session.get("guest_cart", {})

        if not guest_cart:
            return Response({
                "message": "No guest cart to sync."
            }, status=status.HTTP_200_OK)

        # Get or create user's real cart
        cart = Cart.objects.get_or_create_for_request(request)
        merged_count = 0

        for item_key, quantity in guest_cart.items():
            try:
                # Parse item_key → "123_none" or "456_789"
                product_id_str, variant_id_str = item_key.split("_", 1)
                product_id = int(product_id_str)
                variant_id = int(variant_id_str) if variant_id_str != "none" else None
            except (ValueError, AttributeError):
                continue  # Skip malformed keys

            try:
                product = Product.objects.get(id=product_id)
                variant = Variants.objects.get(id=variant_id, product=product) if variant_id else None
            except (Product.DoesNotExist, Variants.DoesNotExist):
                continue  # Skip invalid products

            # Set default delivery option
            default_option = ProductDeliveryOption.objects.filter(
                product=product, default=True
            ).first()

            # Add or update cart item
            cart_item, created = CartItem.objects.get_or_create(
                cart=cart,
                product=product,
                variant=variant,
                defaults={
                    "quantity": quantity,
                    "delivery_option": default_option.delivery_option if default_option else None,
                }
            )

            if not created:
                cart_item.quantity += quantity
                cart_item.save()

            if cart_item.quantity > 0:
                merged_count += 1

        # Clear the guest session cart
        if "guest_cart" in request.session:
            del request.session["guest_cart"]
        request.session.modified = True

        return Response({
            "message": "Guest cart synced successfully.",
            "merged_items": merged_count,
            "total_cart_quantity": cart.total_quantity
        }, status=status.HTTP_200_OK)


##############################################################################################
#######################################     CART VIEW ########################################
##############################################################################################
class CartView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            if request.user.is_authenticated:
                return get_authenticated_cart_response(request)
            else:
                return get_guest_cart_response(request)
        except Exception as e:
            logger.error(f"CartView error: {str(e)}", exc_info=True)
            return Response({
                "detail": "Error loading cart",
                "error": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

##############################################################################################
#######################################     CART VIEW ########################################
##############################################################################################

class NavInfo(APIView):
    """
    Returns user info + cart quantity for the navigation bar
    Works perfectly for:
    - Logged-in users → DB cart
    - Guest users     → Redis session cart (secure & fast)
    """
    permission_classes = [AllowAny]

    def get(self, request):
        is_authenticated = request.user.is_authenticated
        user = request.user if is_authenticated else None
        first_name = user.first_name if is_authenticated and user else None

        # ——————— Cart Quantity ———————
        if is_authenticated:
            try:
                cart = Cart.objects.get_for_request(request)
                cart_quantity = cart.total_quantity if cart else 0
            except Cart.DoesNotExist:
                cart_quantity = 0
        else:
            # Guest: Use Redis session (same as all other views)
            guest_cart = request.session.get("guest_cart", {})
            cart_quantity = sum(guest_cart.values())

        return Response({
            "isAuthenticated": is_authenticated,
            "name": first_name,
            "cartQuantity": cart_quantity,
        })


#####################CHECKOUT##################################
class CheckoutAPIView(APIView):
    def get(self, request):
        user = request.user
        default_address = Address.objects.filter(user=user, status=True).first()
        profile = get_object_or_404(Profile, user=user)
        cart = Cart.objects.get_for_request(request)
        if not cart:
            return Response({"error": "No cart found for this user"}, status=status.HTTP_404_NOT_FOUND)

        # Prioritize Address.country (CharField), then Profile.country, then 'GH'
        buyer_country = default_address.country if default_address and default_address.country else \
                        profile.country if profile and profile.country else 'GH'

        # Check and delete unavailable products
        deleted_items = cart.prevent_checkout_unavailable_products(default_address)

        cart_items = CartItem.objects.filter(cart=cart).select_related(
            'product__vendor__about', 'product__vendor__shipping_from_country', 'delivery_option'
        ).prefetch_related('product__productdeliveryoption_set__delivery_option')
        if not cart_items.exists():
            return Response({"detail": "There are no items to checkout."}, status=status.HTTP_400_BAD_REQUEST)

        # Use default_address or dummy address for FeeCalculator
        address = default_address if default_address else type('DummyAddress', (), {
            'latitude': profile.latitude if profile and profile.latitude else 5.5600,
            'longitude': profile.longitude if profile and profile.longitude else -0.2050,
            'country': buyer_country
        })()

        try:
            total_delivery_fee_result = FeeCalculator.calculate_total_delivery_fee(
                cart_items, address, buyer_country_code=buyer_country
            )
            total_delivery_fee = total_delivery_fee_result.total
            dynamic_quotes = total_delivery_fee_result.dynamic_quotes
            invalid_items = total_delivery_fee_result.invalid_items
        except ValidationError as e:
            logger.error(f"Error calculating total delivery fee: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        delivery_date_ranges = {}
        all_product_delivery_options = {}

        for item in cart_items:
            product = item.product
            vendor = product.vendor
            vendor_country = vendor.shipping_from_country.name if vendor.shipping_from_country else 'GH'
            is_international = buyer_country != vendor_country

            delivery_options_qs = product.productdeliveryoption_set.filter(
                delivery_option__type='international' if is_international else 'local'
            )
            delivery_options = delivery_options_qs.all()

            all_product_delivery_options[product.id] = ProductDeliveryOptionSerializer(
                delivery_options, many=True, context={'request': request}
            ).data

            selected_option = item.selected_delivery_option
            if selected_option:
                # Use ProductDeliveryOption to handle buyer_country
                product_delivery_option = product.productdeliveryoption_set.filter(
                    delivery_option=selected_option
                ).first()
                if product_delivery_option:
                    date_range = product_delivery_option.get_delivery_date_range(
                        reference_date=timezone.now(),
                        buyer_country=buyer_country,
                        dynamic_min_days=dynamic_quotes.get(vendor.id, {}).get('min_days'),
                        dynamic_max_days=dynamic_quotes.get(vendor.id, {}).get('max_days')
                    )
                else:
                    date_range = selected_option.get_delivery_date_range(
                        reference_date=timezone.now(),
                        dynamic_min_days=dynamic_quotes.get(vendor.id, {}).get('min_days'),
                        dynamic_max_days=dynamic_quotes.get(vendor.id, {}).get('max_days')
                    )
            else:
                date_range = "Delivery option not selected"

            delivery_date_ranges[product.id] = date_range

        clipped_coupons = ClippedCoupon.objects.filter(user=user).select_related('coupon')
        applied_coupon = None
        discount_amount = Decimal(0)

        if 'applied_coupon' in request.session:
            try:
                coupon = Coupon.objects.get(id=request.session['applied_coupon'])
                if coupon.is_valid() and clipped_coupons.filter(coupon=coupon).exists():
                    applied_coupon = coupon
                    discount_amount = coupon.discount_amount or (
                        cart.total_price * Decimal(coupon.discount_percentage / 100)
                    ).quantize(Decimal('0.01'))
            except Coupon.DoesNotExist:
                del request.session['applied_coupon']

        response_data = {
            'cart_items': CartItemSerializer(cart_items, many=True, context={'request': request}).data,
            'sub_total': cart.total_price,
            'total_delivery_fee': total_delivery_fee,
            'product_delivery_options': all_product_delivery_options,
            'total_packaging_fee': cart.calculate_packaging_fees(),
            'grand_total': cart.calculate_grand_total() - discount_amount,
            'delivery_date_ranges': delivery_date_ranges,
            'buyer_country': buyer_country,
            'invalid_items': invalid_items,
            'clipped_coupons': CouponSerializer(clipped_coupons, many=True).data,
            'applied_coupon': CouponSerializer(applied_coupon).data if applied_coupon else None,
            'discount_amount': discount_amount,
            'deleted_items': deleted_items,
        }
        return Response(response_data, status=status.HTTP_200_OK)


class UpdateDeliveryOptionAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            # Parse and validate input data
            product_id = request.data.get('product_id')
            delivery_option_id = request.data.get('delivery_option_id')

            if not product_id or not delivery_option_id:
                return Response(
                    {"error": "Product ID and Delivery Option ID are required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Retrieve the cart for the user
            cart = Cart.objects.get_for_request(request)
            if not cart:
                return Response(
                    {"error": "No cart found for this user"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Retrieve the product
            product = get_object_or_404(Product, id=product_id)

            # Retrieve all cart items for the product
            cart_items = CartItem.objects.filter(cart=cart, product=product)
            if not cart_items.exists():
                return Response(
                    {"error": "No items found in the cart for the specified product."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Retrieve the delivery option
            delivery_option = get_object_or_404(DeliveryOption, id=delivery_option_id)

            if not ProductDeliveryOption.objects.filter(
                product=product, delivery_option=delivery_option
            ).exists():
                return Response(
                    {"error": "Selected delivery option is not available for this product."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Get buyer country: Address > Profile > 'GH'
            user = request.user
            default_address = Address.objects.filter(user=user, status=True).first()
            profile = Profile.objects.filter(user=user).first()
            buyer_country = (
                default_address.country if default_address and default_address.country
                else profile.country if profile and profile.country
                else 'GH'
            )

            # Validate international/local delivery for each cart item
            for cart_item in cart_items:
                vendor = cart_item.product.vendor
                vendor_country = (
                    vendor.shipping_from_country.name
                    if vendor and vendor.shipping_from_country
                    else 'GH'
                )
                is_international = buyer_country != vendor_country

                if is_international and delivery_option.type != 'international':
                    return Response(
                        {
                            "error": (
                                f"International delivery required for {cart_item.product.title} "
                                f"to {buyer_country}"
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                elif not is_international and delivery_option.type == 'international':
                    return Response(
                        {
                            "error": (
                                f"Local delivery required for {cart_item.product.title} "
                                f"in {buyer_country}"
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            # Update delivery option for all matching cart items
            cart_items.update(delivery_option=delivery_option)

            return Response(
                {"message": "Delivery option updated successfully for all matching items."},
                status=status.HTTP_200_OK,
            )

        except DeliveryOption.DoesNotExist:
            return Response(
                {"error": "Selected delivery option does not exist."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            logger.error(f"Error updating delivery option: {str(e)}")
            return Response(
                {"error": "An unexpected error occurred."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CartSummaryAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        try:
            cart = Cart.objects.get_for_request(request)
        except Cart.DoesNotExist:
            return Response({'detail': 'Cart not found.'}, status=status.HTTP_404_NOT_FOUND)

        default_address = Address.objects.filter(user=user, status=True).first()
        user_profile = Profile.objects.filter(user=user).first()

        # Get country, latitude, longitude: Address > Profile > Accra
        if default_address and default_address.country and default_address.latitude and default_address.longitude:
            buyer_country = default_address.country
            latitude = default_address.latitude
            longitude = default_address.longitude
        elif user_profile and user_profile.country and user_profile.latitude and user_profile.longitude:
            buyer_country = user_profile.country
            latitude = user_profile.latitude
            longitude = user_profile.longitude
        else:
            buyer_country = 'GH'
            latitude = 5.5600
            longitude = -0.2050
            logger.warning(f"No valid address or profile coordinates for user {user.email}. Using Accra default.")

        # Create address object for FeeCalculator
        address = default_address if default_address else type('DummyAddress', (), {
            'latitude': latitude,
            'longitude': longitude,
            'country': buyer_country
        })()

        try:
            total_delivery_fee_result = FeeCalculator.calculate_total_delivery_fee(
                cart.cart_items.all(), address, buyer_country_code=buyer_country
            )
            total_delivery_fee = total_delivery_fee_result.total
            invalid_items = total_delivery_fee_result.invalid_items
        except ValidationError as e:
            logger.warning(f"Delivery fee calculation failed: {str(e)}. Falling back to zero.")
            total_delivery_fee = Decimal(0)
            invalid_items = []

        summary = {
            "grand_total": round(cart.calculate_grand_total() * exchange_rate, 2) or 0.00,
            "grand_total_cedis": round(cart.calculate_grand_total(), 2) or 0.00,
            "delivery_fee": round(total_delivery_fee * exchange_rate, 2) or 0.00,
            "packaging_fee": round(Decimal(cart.calculate_packaging_fees()) * exchange_rate, 2) or 0.00,
            "total_price": round(Decimal(cart.total_price) * exchange_rate, 2) or 0.00,
            "total_quantity": cart.total_quantity or 0,
            "total_items": cart.total_items or 0,
            "currency": currency,
            "buyer_country": buyer_country,
            "invalid_items": invalid_items,
        }
        return Response(summary, status=status.HTTP_200_OK)



###############################################################
#################### DEFAULT ADDRESS ##########################
###############################################################
class DefaultAddressAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        try:
            address = Address.objects.get(user=user, status=True)
        except Address.DoesNotExist:
            return Response(
                {"detail": "No default address found for this user."},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = AddressSerializer(address, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

def truncate(text, max_length=40):
    return text if len(text) <= max_length else text[:max_length - 3] + "..."


class OrderReceiptAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        currency = request.GET.get('currency') or request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))
        currency_symbol = None

        if currency == 'USD':
            currency_symbol = "$"
        if currency == 'GHS':
            currency_symbol = f"GHS"
        
        try:
            order = Order.objects.get(id=order_id, user=request.user)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=404)

        user_profile = Profile.objects.filter(user=request.user).first()
        if not user_profile:
            return Response({'detail': 'User profile not found.'}, status=400)

        buyer_country = order.address.country or 'GH'

        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="receipt_{order.order_number}.pdf"'

        p = canvas.Canvas(response, pagesize=A4)
        width, height = A4
        margin = 50
        y = height - margin

        # === Logo ===
        logo_path = os.path.join(settings.BASE_DIR, "static", "logo-1.png")
        try:
            if os.path.exists(logo_path):
                logo_width = 120
                logo_height = 60
                x_pos = width - logo_width - margin
                y_pos = y - (logo_height / 2) + 6
                p.drawImage(logo_path, x_pos, y_pos, width=logo_width, height=logo_height, preserveAspectRatio=True)
        except Exception as e:
            logger.warning(f"Logo error: {e}")

        # === Header ===
        p.setFont("Helvetica-Bold", 18)
        p.drawString(margin, y, "ORDER RECEIPT")
        y -= 25
        p.setFont("Helvetica", 12)
        p.drawString(margin, y, f"Order Number: {order.order_number}")
        y -= 15
        p.drawString(margin, y, f"Order ID: {order.id}")
        y -= 15
        p.drawString(margin, y, f"Date: {order.date_created.strftime('%d %B %Y')}")
        y -= 30

        # === Customer Info ===
        p.setFont("Helvetica-Bold", 13)
        p.drawString(margin, y, "Customer Information")
        y -= 15
        p.setFont("Helvetica", 11)
        p.drawString(margin, y, f"Name: {order.user.first_name} {order.user.last_name}")
        y -= 15
        p.drawString(margin, y, f"Email: {order.user.email}")
        y -= 15
        country_name = Country.objects.filter(code=buyer_country).first().name if Country.objects.filter(code=buyer_country).exists() else buyer_country
        address_str = f"{order.address.address}, {order.address.town}, {order.address.region}, {country_name} ({buyer_country})"
        p.drawString(margin, y, f"Address: {address_str}")
        y -= 30

        # === Payment Info ===
        p.setFont("Helvetica-Bold", 13)
        p.drawString(margin, y, "Payment Information")
        y -= 15
        p.setFont("Helvetica", 11)
        p.drawString(margin, y, f"Payment Method: {order.payment_method.title().replace('_', ' ')}")
        y -= 15
        p.drawString(margin, y, f"Status: {order.status.title()}")
        y -= 30

        # === Table Header ===
        p.setFont("Helvetica-Bold", 12)
        p.drawString(margin, y, "Item")
        p.drawString(margin + 250, y, "Qty")
        p.drawString(margin + 300, y, "Unit Price")
        p.drawString(margin + 400, y, "Subtotal")
        y -= 10
        p.line(margin, y, width - margin, y)
        y -= 15

        p.setFont("Helvetica", 10)
        for item in order.order_products.all():
            if y < 100:
                p.showPage()
                y = height - margin
                p.setFont("Helvetica", 10)

            p.drawString(margin, y, truncate(item.product.title))
            p.drawString(margin + 250, y, str(item.quantity))
            converted_price = Decimal(item.price) * exchange_rate
            converted_amount = Decimal(item.amount) * exchange_rate
            p.drawString(margin + 300, y, f"{currency_symbol} {converted_price:,.2f}")
            p.drawString(margin + 400, y, f"{currency_symbol} {converted_amount:,.2f}")
            y -= 15

            details = []
            if item.variant:
                if item.variant.size:
                    details.append(f"Size: {item.variant.size.name}")
                if item.variant.color:
                    details.append(f"Color: {item.variant.color.name}")
            if item.selected_delivery_option:
                delivery_option = item.selected_delivery_option
                is_international = buyer_country != (item.product.vendor.shipping_from_country.name if item.product.vendor.shipping_from_country else 'GH')
                delivery_str = delivery_option.name
                if is_international and item.delivery_provider:
                    delivery_str += f" (via {item.delivery_provider})"
                details.append(f"Delivery: {delivery_str}")
            if details:
                p.setFont("Helvetica-Oblique", 8)
                p.drawString(margin + 15, y, "(" + ", ".join(details) + ")")
                y -= 13
                p.setFont("Helvetica", 10)

        y -= 10
        p.line(margin, y, width - margin, y)
        y -= 20

        # === Vendor Breakdown ===
        p.setFont("Helvetica-Bold", 12)
        p.drawString(margin, y, "Vendor Details")
        y -= 15
        p.setFont("Helvetica", 10)

        grand_delivery = Decimal(0)
        for vendor in order.vendors.all():
            if y < 100:
                p.showPage()
                y = height - margin
                p.setFont("Helvetica", 10)

            vendor_delivery = order.get_vendor_delivery_cost(vendor)
            delivery_range = order.get_vendor_delivery_date_range(vendor)
            vendor_country = vendor.shipping_from_country.name if vendor.shipping_from_country else 'GH'
            is_international = buyer_country != vendor_country
            provider = order.order_products.filter(product__vendor=vendor).first().delivery_provider if is_international else None

            converted_delivery = Decimal(vendor_delivery) * exchange_rate
            grand_delivery += converted_delivery

            p.drawString(margin, y, f"Seller: {vendor.name}")
            y -= 15
            p.drawString(margin + 15, y, f"Email: {vendor.email}")
            y -= 15
            p.drawString(margin + 15, y, f"Contact: {vendor.contact}")
            y -= 15
            # delivery_label = f"Delivery ({provider if provider else 'Local'})"
            p.drawString(margin + 15, y, f"Range(ETA): {delivery_range}")
            y -= 25

        # === Total Summary ===
        p.setFont("Helvetica-Bold", 11)
        subtotal = Decimal(order.total_price) * exchange_rate
        grand_total = Decimal(order.calculate_grand_total()) * exchange_rate
        delivery = order.calculate_total_delivery_fee().total * exchange_rate

        p.drawString(margin + 320, y, "Subtotal:")
        p.drawString(margin + 420, y, f"{currency_symbol} {subtotal:,.2f}")
        y -= 15
        p.drawString(margin + 320, y, "Delivery:")
        p.drawString(margin + 420, y, f"{currency_symbol} {delivery:,.2f}")
        y -= 15
        p.drawString(margin + 320, y, "Grand Total:")
        p.drawString(margin + 420, y, f"{currency_symbol} {grand_total:,.2f}")

        # === Footer ===
        y -= 40
        p.setFont("Helvetica-Oblique", 10)
        p.drawString(margin, y, "Thank you for your order! For questions, contact support@negromart.com")

        p.showPage()
        p.save()
        return response
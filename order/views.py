from decimal import Decimal
from django.shortcuts import get_object_or_404
from rest_framework import status, views

from address.models import Address
from address.serializers import AddressSerializer
from order.service import calculate_delivery_fee
from .models import Cart, CartItem, Order, OrderProduct
from product.models import Product, Variants
from rest_framework.response import Response
from django.utils.crypto import get_random_string
from django.core.exceptions import ObjectDoesNotExist
from product.models import Product, ProductDeliveryOption
from rest_framework.permissions import IsAuthenticated
from .serializers import *
from product.utils import *
from userauths.models import User
from django.http import HttpResponse
from reportlab.pdfgen import canvas
import os
from django.conf import settings

from order.service import FeeCalculator
from rest_framework.permissions import IsAuthenticated
from django.http import JsonResponse
from rest_framework import status, views
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from order.models import Cart, CartItem
from product.models import Product, Variants, ProductDeliveryOption
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView
from .cart_utils import get_authenticated_cart_response, get_guest_cart_response

logger = logging.getLogger(__name__)


class AddToCartView(views.APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        data = request.data

        product_id = data.get("product_id")
        variant_id = data.get("variant_id")
        quantity = int(data.get("quantity"))
        is_in_cart = False

        try:
            product = Product.objects.get(id=product_id)
        except Product.DoesNotExist:
            raise NotFound(detail="Product not found.")

        # Fetch the product
        product = get_object_or_404(Product, id=product_id)

        # Fetch variant (if applicable)
        variant = get_object_or_404(Variants, id=variant_id, product=product) if variant_id else None
        cart = Cart.objects.get_or_create_for_request(request)

        default_delivery_option = ProductDeliveryOption.objects.filter(
            product=product, default=True
        ).first()

        cart_item, created = CartItem.objects.get_or_create(
            cart=cart,
            product=product,
            variant=variant,
            defaults={
                "quantity": quantity,
                "delivery_option": default_delivery_option.delivery_option if default_delivery_option else None,
            },
        )

        if not created:
            # If the item already exists, increase the quantity
            cart_item.quantity += quantity
            cart_item.save()

        # should delete the cart item if it gets to 0
        if cart_item.quantity < 1:
            cart_item.delete()
            is_in_cart = False
            message = "Item removed from cart."
            res_quantity = 0
        else:
            is_in_cart = True
            res_quantity = cart_item.quantity
            if created:
                message = "Item added to cart."
            elif quantity > 0:
                message = "Item quantity increased."
            else:
                message = "Item quantity decreased."

        variant = Variants.objects.get(id=variant_id) if variant_id else Variants.objects.filter(product=product).first()
        is_out_of_stock = False
        stock_quantity = 0
        if product.variant == 'None':
            stock_quantity = product.total_quantity
            is_out_of_stock = stock_quantity < 1
        else:
            if variant:
                stock_quantity = variant.quantity
                is_out_of_stock = stock_quantity < 1
            else:
                # If the product uses variants but no variant is selected
                is_out_of_stock = True

        if cart_item.quantity >= stock_quantity and stock_quantity != 0:
                is_out_of_stock = True

        return Response({
            "message": message,
            "quantity": res_quantity,
            "is_in_cart": is_in_cart,
            "is_out_of_stock": is_out_of_stock,
        })

class RemoveFromCartView(views.APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        data = request.data
        # Validate required fields
        product_id = data.get("product_id")
        if not product_id:
            return Response(
                {"error": "product_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        variant_id = data.get("variant_id", None)

        try:
            # Get or create the cart (handles both authenticated and guest users)
            cart = Cart.objects.get_for_request(request)

            # Fetch the product
            product = get_object_or_404(Product, id=product_id)

            # Fetch variant (if applicable)
            variant = None
            if variant_id:
                variant = get_object_or_404(Variants, id=variant_id)
                # Verify variant belongs to product
                if variant.product != product:
                    return Response(
                        {"error": "Variant does not belong to product"},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Find and remove the cart item
            cart_item = get_object_or_404(
                CartItem,
                cart=cart,
                product=product,
                variant=variant
            )
            cart_item.delete()

            # Prepare updated cart data for response
            cart_items = CartItem.objects.filter(cart=cart).select_related('product', 'variant')
            packaging_fee = sum(item.product.packaging_fee * item.quantity for item in cart_items)

            response_data = {
                "success": True,
                "message": "Item removed from cart",
                "quantity": cart.total_quantity if cart else 0,
                "cart": {
                    "items_count": cart_items.count(),
                    "total_amount": cart.total_price if cart else 0,
                    "packaging_fee": packaging_fee,
                }
            }

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


##############################################################################################
#######################################     CART QUANTITY ####################################
##############################################################################################
class CartQuantityView(APIView):
    """Retrieve the total quantity of items in the user's cart or guest cart."""

    def get(self, request):
        try:
            if request.user.is_authenticated:  # Authenticated user
                cart = Cart.objects.get_for_request(request)
                total_quantity = cart.total_quantity if cart else 0
            else:  # Guest user
                guest_cart_header = request.headers.get('X-Guest-Cart')
                try:
                    guest_cart = json.loads(guest_cart_header) if guest_cart_header else []
                except (json.JSONDecodeError, TypeError):
                    guest_cart = []

                total_quantity = sum(int(item.get("q", 0)) for item in guest_cart)

            return Response({"quantity": total_quantity}, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Cart quantity view error: {str(e)}", exc_info=True)
            return Response(
                {"detail": "Error retrieving cart quantity", "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
##############################################################################################
#######################################     CART QUANTITY ####################################
##############################################################################################

class SyncGuestCartView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            guest_cart = request.headers.get('X-Guest-Cart', '[]')

            try:
                cart_items = json.loads(guest_cart)
            except (json.JSONDecodeError, TypeError):
                cart_items = []

            if not cart_items:
                response = Response(
                    {"message": "No guest cart items to sync."},
                    status=status.HTTP_200_OK
                )
                response.delete_cookie('guest_cart')
                return response

            cart = Cart.objects.get_or_create_for_request(request)

            for item in cart_items:
                product_id = item.get("p")
                quantity = int(item.get("q", 0))
                variant_id = item.get("v")

                if not product_id or quantity <= 0:
                    continue

                try:
                    product = Product.objects.get(id=product_id)
                    variant = (
                        Variants.objects.get(id=variant_id, product=product)
                        if variant_id else None
                    )
                except (Product.DoesNotExist, Variants.DoesNotExist):
                    continue

                default_delivery_option = ProductDeliveryOption.objects.filter(
                    product=product, default=True
                ).first()

                cart_item, created = CartItem.objects.get_or_create(
                    cart=cart,
                    product=product,
                    variant=variant,
                    defaults={
                        "quantity": quantity,
                        "delivery_option": default_delivery_option.delivery_option if default_delivery_option else None,
                    },
                )

                if not created:
                    cart_item.quantity += quantity
                    cart_item.save()

                if cart_item.quantity < 1:
                    cart_item.delete()

            response = Response(
                {"message": "Guest cart synced successfully."},
                status=status.HTTP_200_OK
            )
            response.delete_cookie('guest_cart')
            return response

        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


##############################################################################################
#######################################     CART VIEW ########################################
##############################################################################################
class CartView(APIView):
    def get(self, request):
        try:
            if request.auth and request.user.is_authenticated:
                return get_authenticated_cart_response(request)
            else:
                return get_guest_cart_response(request)

        except Exception as e:
            logger.error(f"Cart view error: {str(e)}", exc_info=True)
            return Response(
                {"detail": "Error loading cart", "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

##############################################################################################
#######################################     CART VIEW ########################################
##############################################################################################

class NavInfo(APIView):

    def get(self, request):
        user = User.objects.filter(id=request.user.id).first()
        is_authenticated = request.user.is_authenticated

        # If user is authenticated, you can serialize more info
        if request.auth:  # Authenticated user
            cart = Cart.objects.get_for_request(request)
            total_quantity = cart.total_quantity if cart else 0

        else:  # Guest user
            guest_cart_header = request.headers.get('X-Guest-Cart')
            try:
                guest_cart = json.loads(guest_cart_header) if guest_cart_header else []
            except (json.JSONDecodeError, TypeError):
                guest_cart = []

            total_quantity = sum(int(item.get("q", 0)) for item in guest_cart)

        return Response({
            "isAuthenticated": is_authenticated,
            "name": user.first_name if is_authenticated else None,
            "cartQuantity": total_quantity,
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
from reportlab.lib import colors
from reportlab.lib.units import inch

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
from decimal import Decimal
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404
from rest_framework import status, views

from address.models import Address
from address.serializers import AddressSerializer
from .serializers import OrderSerializer
from order.service import calculate_delivery_fee
from .models import Cart, CartItem, Order, OrderProduct
from product.models import Product, Variants
from rest_framework.response import Response
from django.utils.crypto import get_random_string
from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse
from product.models import Product, ProductDeliveryOption
from rest_framework.permissions import IsAuthenticated
from rest_framework.permissions import AllowAny  # Optional, depending on your auth setup
from .serializers import *
from product.utils import *
from userauths.models import User
from userauths.authentication import CustomJWTAuthentication
from django.utils.timezone import now
from django.http import HttpResponse
from reportlab.pdfgen import canvas
import os
from django.conf import settings


from rest_framework.permissions import IsAuthenticated
from django.http import JsonResponse
from rest_framework import status, views
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from order.models import Cart, CartItem
from product.models import Product, Variants, ProductDeliveryOption
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView
from product.serializers import ProductSerializer, VariantSerializer
from .cart_utils import get_authenticated_cart_response, get_guest_cart_response, calculate_packaging_fee

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
            if request.auth and request.user.is_authenticated:  # Authenticated user
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


###############################################################
#####################CHECKOUT##################################
###############################################################

class CheckoutAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        profile = get_object_or_404(Profile, user=user)

        cart = Cart.objects.get_for_request(request)
        if not cart:
            return Response({"error": "No cart found for this user"}, status=status.HTTP_404_NOT_FOUND)

        cart_items = CartItem.objects.filter(cart=cart)
        if not cart_items.exists():
            return Response({"detail": "There are no items to checkout."}, status=status.HTTP_400_BAD_REQUEST)

        total_delivery_fee = Decimal(0)
        delivery_date_ranges = {}
        all_product_delivery_options = {}

        for item in cart_items:
            product = item.product
            vendor = product.vendor

            # Get all delivery options for this product
            product_delivery_options = ProductDeliveryOption.objects.filter(product=product)
            if not product_delivery_options.exists():
                return Response({
                    "detail": f"No delivery options found for product {product.title}"
                }, status=status.HTTP_400_BAD_REQUEST)

            # Serialize all delivery options for frontend dropdown
            all_product_delivery_options[product.id] = ProductDeliveryOptionSerializer(
                product_delivery_options, many=True,
                context={'request': request}
            ).data

            # Select delivery option: user-chosen or fallback to default
            selected_delivery_option = item.delivery_option

            if not selected_delivery_option:
                default_option = product_delivery_options.filter(default=True).first()
                if default_option:
                    selected_delivery_option = default_option.delivery_option

            if selected_delivery_option:
                # Calculate delivery fee
                delivery_fee = calculate_delivery_fee(
                    vendor.about.latitude,
                    vendor.about.longitude,
                    profile.latitude,
                    profile.longitude,
                    selected_delivery_option.cost
                )
                total_delivery_fee += Decimal(delivery_fee)

                min_date = now() + timezone.timedelta(days=selected_delivery_option.min_days)
                max_date = now() + timezone.timedelta(days=selected_delivery_option.max_days)

                # Estimate delivery date range
                if selected_delivery_option.min_days == selected_delivery_option.max_days:
                    if selected_delivery_option.min_days == 0:
                        label = "Today"
                    elif selected_delivery_option.min_days == 1:
                        label = "Tomorrow"
                    else:
                        label = f"In {selected_delivery_option.min_days} days"
                else:
                    label = f"{min_date.strftime('%d %B')} to {max_date.strftime('%d %B')}"

                delivery_date_ranges[product.id] = label
            else:
                delivery_date_ranges[product.id] = "Delivery option not selected"

        # Coupons
        clipped_coupons = ClippedCoupon.objects.filter(user=user)
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

        grand_total = cart.calculate_grand_total(profile) - discount_amount

        response_data = {
            'cart_items': CartItemSerializer(cart_items, many=True, context={'request': request}).data,
            'sub_total': cart.total_price,
            'total_delivery_fee': total_delivery_fee,
            'product_delivery_options': all_product_delivery_options,
            'total_packaging_fee': cart.calculate_packaging_fees(),
            'grand_total': grand_total,
            'delivery_date_ranges': delivery_date_ranges,
            'clipped_coupons': CouponSerializer(clipped_coupons, many=True).data,
            'applied_coupon': CouponSerializer(applied_coupon).data if applied_coupon else None,
            'discount_amount': discount_amount,
        }

        return Response(response_data, status=status.HTTP_200_OK)


class UpdateDeliveryOptionAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            # Parse data from the request
            product_id = request.data.get('product_id')
            delivery_option_id = request.data.get('delivery_option_id')

            # Validate inputs
            if not product_id or not delivery_option_id:
                return Response(
                    {"error": "Product ID and Delivery Option ID are required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Retrieve or create the cart for the user
            cart = Cart.objects.get_for_request(request)

            if not cart:
                return Response(
                    {"error": "No cart found for this user"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Retrieve all cart items for the product ID within the user's cart
            cart_items = CartItem.objects.filter(cart=cart, product__id=product_id)

            if not cart_items.exists():
                return Response(
                    {"error": "No items found in the cart for the specified product."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Validate the delivery option
            delivery_option = get_object_or_404(DeliveryOption, id=delivery_option_id)

            # Update the delivery option for all matching cart items
            cart_items.update(delivery_option=delivery_option)

            return Response(
                {
                    "message": "Delivery option updated successfully for all matching items.",
                },
                status=status.HTTP_200_OK,
            )

        except DeliveryOption.DoesNotExist:
            return Response(
                {"error": "Selected delivery option does not exist."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CartSummaryAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user if request.user.is_authenticated else None

        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        try:
            if user:
                cart = Cart.objects.get_for_request(request)
            else:
                return Response({'detail': 'No cart found.'}, status=status.HTTP_404_NOT_FOUND)

            user_profile = Profile.objects.get(user=user) if user else None

            if not user_profile:
                return Response({'detail': 'User profile not found.'}, status=status.HTTP_400_BAD_REQUEST)

            # Fallback lat/lon if not set
            if not user_profile.latitude:
                user_profile.latitude = 5.5600  # Accra
            if not user_profile.longitude:
                user_profile.longitude = -0.2050  # Accra

            if not user_profile:
                return Response({'detail': 'User profile not found.'}, status=status.HTTP_400_BAD_REQUEST)

            summary = {
                "grand_total": round(cart.calculate_grand_total(user_profile) * exchange_rate, 2) or 0.00,
                "grand_total_cedis": round(cart.calculate_grand_total(user_profile), 2) or 0,
                "delivery_fee": round(cart.calculate_total_delivery_fee(user_profile) * exchange_rate, 2) or 0.00,
                "packaging_fee": round(Decimal(cart.calculate_packaging_fees()) * exchange_rate, 2) or 0.00,
                "total_price": round(Decimal(cart.total_price) * exchange_rate, 2) or 0.00,
                "total_quantity": cart.total_quantity or 0,
                "total_items": cart.total_items or 0,
                "currency": currency,
            }

            return Response(summary, status=status.HTTP_200_OK)

        except Cart.DoesNotExist:
            return Response({'detail': 'Cart not found.'}, status=status.HTTP_404_NOT_FOUND)
        except Profile.DoesNotExist:
            return Response({'detail': 'User profile not found.'}, status=status.HTTP_400_BAD_REQUEST)



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

        currency_symbol = "$" if currency == "USD" else "₵"  # 👈 UPDATED

        try:
            order = Order.objects.get(id=order_id, user=request.user)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=404)
        
        user_profile = Profile.objects.filter(user=request.user).first()

        if not user_profile:
            return Response({'detail': 'User profile not found.'}, status=status.HTTP_400_BAD_REQUEST)

        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="receipt_{order.order_number}.pdf"'

        p = canvas.Canvas(response, pagesize=A4)
        width, height = A4
        margin = 50
        y = height - margin

        # === Logo (Optional) ===
        logo_path = os.path.join(settings.BASE_DIR, "static", "assets", "imgs", "logo.png")
        try:
            if os.path.exists(logo_path):
                logo_width = 120
                logo_height = 60
                margin = 50
                x_pos = width - logo_width - margin
                y_pos = y - (logo_height / 2) + 6  # Adjust vertically to align with text baseline

                p.drawImage(logo_path, x_pos, y_pos, width=logo_width, height=logo_height, preserveAspectRatio=True)
        except Exception as e:
            print("Logo error:", e)

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
        p.drawString(margin, y, f"Address: {order.address.address}, {order.address.town}, {order.address.region}, {order.address.country}")
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

            # Product title
            p.drawString(margin, y, truncate(item.product.title))
            p.drawString(margin + 250, y, str(item.quantity))
            converted_price = Decimal(item.price) * exchange_rate  # 👈 UPDATED
            converted_amount = Decimal(item.amount) * exchange_rate  # 👈 UPDATED

            p.drawString(margin + 300, y, f"{currency_symbol} {converted_price:,.2f}")  # 👈 UPDATED
            p.drawString(margin + 400, y, f"{currency_symbol} {converted_amount:,.2f}") 
            y -= 15

            # Variant details
            if item.variant:
                variant_details = []
                if item.variant.size:
                    variant_details.append(f"Size: {item.variant.size.name}")
                if item.variant.color:
                    variant_details.append(f"Color: {item.variant.color.name}")
                if variant_details:
                    p.setFont("Helvetica-Oblique", 8)
                    p.drawString(margin + 15, y, "(" + ", ".join(variant_details) + ")")
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

        
        grand_total = Decimal(order.total)
        grand_delivery = Decimal(0)

        for vendor in order.vendors.all():
            if y < 100:
                p.showPage()
                y = height - margin
                p.setFont("Helvetica", 10)

            vendor_delivery = order.get_vendor_delivery_cost(vendor)
            delivery_range = order.get_vendor_delivery_date_range(vendor)

            grand_delivery += Decimal(vendor_delivery)

            p.drawString(margin, y, f"Seller: {vendor.name}")
            y -= 15
            p.drawString(margin + 15, y, f"Email: {vendor.email}")
            y -= 15
            p.drawString(margin + 15, y, f"Contact: {vendor.contact}")
            y -= 15
            p.drawString(margin + 15, y, f"Delivery Range: {delivery_range}")
            y -= 25

        # === Total Summary ===
        p.setFont("Helvetica-Bold", 11)
        subtotal = Decimal(order.total_price) * exchange_rate  # 👈 UPDATED
        delivery = Decimal(order.calculate_total_delivery_fee(user_profile)) * exchange_rate  # 👈 UPDATED
        grand_total = Decimal(order.calculate_grand_total(user_profile)) * exchange_rate  # 👈 UPDATED

        p.drawString(margin + 320, y, "Subtotal:")
        p.drawString(margin + 420, y, f"{currency_symbol} {subtotal:,.2f}")  # 👈 UPDATED
        y -= 15
        p.drawString(margin + 320, y, "Delivery:")
        p.drawString(margin + 420, y, f"{currency_symbol} {delivery:,.2f}")  # 👈 UPDATED
        y -= 15
        p.drawString(margin + 320, y, "Grand Total:")
        p.drawString(margin + 420, y, f"{currency_symbol} {grand_total:,.2f}")  # 👈 UPDATED

        # === Footer ===
        y -= 40
        p.setFont("Helvetica-Oblique", 10)
        p.drawString(margin, y, "Thank you for your order! For questions, contact support@example.com")

        p.showPage()
        p.save()
        return response

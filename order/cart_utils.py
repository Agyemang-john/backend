
import logging
from rest_framework.response import Response
from product.models import Product, Variants
from .models import Cart
from product.serializers import ProductSerializer, VariantSerializer
from .serializers import CartItemSerializer
from core.service import get_exchange_rates
from decimal import Decimal

from decimal import InvalidOperation
import logging

logger = logging.getLogger(__name__)

def calculate_packaging_fee(weight, volume):
    # Example rates, adjust as needed
    weight_rate = Decimal('1.0')  # Packaging fee per kg
    volume_rate = Decimal('1.0')  # Packaging fee per cubic meter

    # Handle None or invalid weight/volume
    try:
        weight = Decimal(str(weight)) if weight is not None else Decimal('0')
        volume = Decimal(str(volume)) if volume is not None else Decimal('0')
    except (InvalidOperation, TypeError) as e:
        logger.error(f"Invalid weight or volume: weight={weight}, volume={volume}, error={e}")
        return Decimal('0')

    weight_fee = weight * weight_rate
    volume_fee = volume * volume_rate

    # Choose the higher fee or sum both if needed
    packaging_fee = weight_fee + volume_fee
    return packaging_fee


def get_authenticated_cart_response(request):
    cart = Cart.objects.prefetch_related('cart_items__product', 'cart_items__variant').get_or_create(user=request.user)
    currency = request.headers.get('X-Currency', 'GHS')
    exchange_rate = Decimal(str(get_exchange_rates().get(currency, 1)))

    return Response({
        "items": CartItemSerializer(
            cart.cart_items.all(),
            many=True,
            context={'request': request}
        ).data,
        "total_amount": round(cart.total_price * exchange_rate, 2),
        "packaging_fee": round(cart.calculate_packaging_fees() * exchange_rate, 2),
        "cart_id": cart.id,
        "currency": currency,
        "is_guest": False
    })


def get_guest_cart_response(request):
    guest_cart = request.session.get("guest_cart", {})
    if not guest_cart:
        return Response({
            "items": [],
            "total_amount": 0,
            "packaging_fee": 0,
            "cart_id": None,
            "currency": request.headers.get('X-Currency', 'GHS'),
            "is_guest": True
        })

    currency = request.headers.get('X-Currency', 'GHS')
    exchange_rate = Decimal(str(get_exchange_rates().get(currency, 1)))
    items = []
    total_amount = Decimal('0')
    packaging_fee = Decimal('0')

    for item_key, quantity in guest_cart.items():
        try:
            product_id_str, variant_id_str = item_key.split("_", 1)
            product_id = int(product_id_str)
            variant_id = int(variant_id_str) if variant_id_str != "none" else None
        except (ValueError, AttributeError):
            continue

        try:
            product = Product.objects.get(id=product_id, status="published")
            variant = Variants.objects.get(id=variant_id, product=product) if variant_id else None
            price = variant.price if variant else product.price
            price = Decimal(str(price))
        except (Product.DoesNotExist, Variants.DoesNotExist):
            continue

        subtotal = price * quantity
        item_packaging = calculate_packaging_fee(product.weight, product.volume) * quantity

        items.append({
            "product": ProductSerializer(product, context={'request': request}).data,
            "variant": VariantSerializer(variant, context={'request': request}).data if variant else None,
            "quantity": quantity,
            "subtotal": float(subtotal),
            "item_packaging_fee": float(item_packaging),
        })

        total_amount += subtotal
        packaging_fee += item_packaging

    return Response({
        "items": items,
        "total_amount": round(total_amount * exchange_rate, 2),
        "packaging_fee": round(packaging_fee * exchange_rate, 2),
        "cart_id": None,
        "currency": currency,
        "is_guest": True
    })

from .models import CartItem
from product.models import Product, Variants, ProductDeliveryOption


def handle_authenticated_cart(user, product, variant, quantity_change):
    cart, _ = Cart.objects.get_or_create(user=user)

    cart_item, created = CartItem.objects.get_or_create(
        cart=cart,
        product=product,
        variant=variant,
        defaults={"quantity": 0}
    )

    old_quantity = cart_item.quantity
    new_quantity = old_quantity + quantity_change

    if new_quantity <= 0:
        cart_item.delete()
        return {
            "message": "Item removed from cart.",
            "quantity": 0,
            "is_in_cart": False,
            "cart_item_id": None
        }

    # Update quantity
    cart_item.quantity = new_quantity

    # Set delivery option only on first add
    if created or not cart_item.delivery_option:
        default_option = ProductDeliveryOption.objects.filter(product=product, default=True).first()
        cart_item.delivery_option = default_option.delivery_option if default_option else None

    cart_item.save()

    # Exact messages you wanted
    if created:
        message = "Item added to cart."
    elif quantity_change > 0:
        message = "Item quantity increased."
    else:
        message = "Item quantity decreased."

    return {
        "message": message,
        "quantity": new_quantity,
        "is_in_cart": True,
        "cart_item_id": cart_item.id
    }


def handle_guest_cart(session, item_key, quantity_change):
    guest_cart = session.get("guest_cart", {})
    old_quantity = guest_cart.get(item_key, 0)
    new_quantity = old_quantity + quantity_change

    if new_quantity <= 0:
        guest_cart.pop(item_key, None)
        session["guest_cart"] = guest_cart
        session.modified = True
        return {
            "message": "Item removed from cart.",
            "quantity": 0,
            "is_in_cart": False
        }

    guest_cart[item_key] = new_quantity
    session["guest_cart"] = guest_cart
    session.modified = True

    # Same exact messages for guest
    if old_quantity == 0:
        message = "Item added to cart."
    elif quantity_change > 0:
        message = "Item quantity increased."
    else:
        message = "Item quantity decreased."

    return {
        "message": message,
        "quantity": new_quantity,
        "is_in_cart": True
    }

def remove_from_guest_cart(session, item_key):
    """Remove item from guest session cart"""
    guest_cart = session.get("guest_cart", {})
    removed = item_key in guest_cart
    guest_cart.pop(item_key, None)
    session["guest_cart"] = guest_cart
    session.modified = True
    return removed


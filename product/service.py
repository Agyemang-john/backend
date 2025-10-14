from order.models import Order, OrderProduct, CartItem, Cart
from product.models import Product
from django.db.models import Count
from django.utils import timezone
from datetime import timedelta

from collections import Counter
from .models import FrequentlyBoughtTogether, Product

import json
from order.models import OrderProduct
from product.models import Product
from django.db.models import Count
from django.utils import timezone
from datetime import timedelta


def get_recommended_products(request):
    user = request.auth
    guest_cart_raw = request.headers.get('X-Guest-Cart')

    recommended_ids = set()

    if user:
        # Products ordered by user
        user_products = OrderProduct.objects.filter(order__user=request.user).values_list('product_id', flat=True)
        print(f"User's ordered products: {user_products}")

        # Orders where those products appear (co-occurrence)
        related_order_ids = OrderProduct.objects.filter(product_id__in=user_products).values_list('order_id', flat=True)

        # Other products often ordered together
        co_ordered = OrderProduct.objects.filter(order_id__in=related_order_ids)\
                        .exclude(product_id__in=user_products)\
                        .values('product_id')\
                        .annotate(freq=Count('product_id'))\
                        .order_by('-freq')[:10]
        recommended_ids.update([item['product_id'] for item in co_ordered])

        # Category-based suggestions
        categories = Product.objects.filter(id__in=user_products).values_list('sub_category', flat=True)
        category_based = Product.objects.filter(sub_category__in=categories)\
                                        .exclude(id__in=user_products)[:10]
        recommended_ids.update(category_based.values_list('id', flat=True))

    elif guest_cart_raw:
        try:
            cart_items = json.loads(guest_cart_raw)  # List of {"p": productId, "q": quantity, ...}
            product_ids = [item['p'] for item in cart_items]

            if product_ids:
                categories = Product.objects.filter(id__in=product_ids).values_list('sub_category', flat=True)
                guest_recommendations = Product.objects.filter(sub_category__in=categories)\
                                                    .exclude(id__in=product_ids)[:10]
                recommended_ids.update(guest_recommendations.values_list('id', flat=True))
        except json.JSONDecodeError:
            pass  # Ignore if cookie is malformed

    # Fallback to trending if no personalized data found
    if not recommended_ids:
        last_week = timezone.now() - timedelta(days=7)
        trending = Product.objects.filter(
            cartitem__date__gte=last_week
        ).annotate(count=Count('cartitem')).order_by('-count')[:10]
        recommended_ids.update(trending.values_list('id', flat=True))

    return Product.objects.filter(id__in=recommended_ids)[:10]




def get_trending_products():
    last_week = timezone.now() - timedelta(days=7)
    trending = CartItem.objects.filter(date__gte=last_week).values('product_id')\
                .annotate(count=Count('product_id')).order_by('-count')[:10]
    return Product.objects.filter(id__in=[t['product_id'] for t in trending])

# Example: Given a product, find other products added to the same cart
def get_cart_based_recommendations(product_id):
    related_cart_ids = CartItem.objects.filter(product_id=product_id).values_list('cart_id', flat=True)
    related_product_ids = CartItem.objects.filter(cart_id__in=related_cart_ids).exclude(product_id=product_id)\
                          .values('product_id').annotate(freq=Count('product_id')).order_by('-freq')[:10]
    return Product.objects.filter(id__in=[r['product_id'] for r in related_product_ids])


def get_category_based_recommendations(user):
    product_ids = OrderProduct.objects.filter(order__user=user).values_list('product_id', flat=True)
    categories = Product.objects.filter(id__in=product_ids).values_list('category', flat=True)
    
    return Product.objects.filter(category__in=categories).exclude(id__in=product_ids)[:10]


def get_fbt_recommendations(cart_product_ids):
    recommended = []

    for product_id in cart_product_ids:
        related = FrequentlyBoughtTogether.objects.filter(
            product_id=product_id
        ).values_list('recommended_id', flat=True)

        recommended.extend(related)

    counter = Counter(recommended)

    # Get top 10 most frequent
    top_related_ids = [item[0] for item in counter.most_common(10)]

    # Exclude items already in the cart
    final_ids = [pid for pid in top_related_ids if pid not in cart_product_ids]

    return Product.objects.filter(id__in=final_ids)


def get_cart_product_ids(request):
    """
    Return a list of product IDs in the user's cart — handles both guest and authenticated users.
    """
    product_ids = []

    # Check if authenticated
    if request.user.is_authenticated:
        cart = Cart.objects.get_for_request(request)
        if cart:
            product_ids = [item.product.id for item in cart.cart_items.all()]
    else:
        # Handle guest cart (stored in cookie header)
        guest_cart_header = request.headers.get('X-Guest-Cart')
        try:
            guest_cart = json.loads(guest_cart_header) if guest_cart_header else []
        except (json.JSONDecodeError, TypeError):
            guest_cart = []

        for item in guest_cart:
            try:
                product_id = item.get("p")
                if product_id:
                    product_ids.append(int(product_id))
            except Exception as e:
                continue

    return product_ids

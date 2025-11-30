from order.models import CartItem, Cart
from product.models import Product
from django.db.models import Count
from django.utils import timezone
from datetime import timedelta

from collections import Counter
from .models import FrequentlyBoughtTogether, Product

def get_cart_product_ids(request):
    """
    Returns list of product IDs in cart — works for BOTH guest (session) and logged-in (DB)
    """
    product_ids = []

    if request.user.is_authenticated:
        try:
            cart = Cart.objects.get(user=request.user)
            product_ids = [item.product.id for item in cart.cart_items.all()]
        except Cart.DoesNotExist:
            pass
    else:
        # GUEST: Use Redis session — NO headers, NO cookies
        guest_cart = request.session.get("guest_cart", {})
        for item_key in guest_cart.keys():
            try:
                product_id_str, _ = item_key.split("_", 1)
                product_ids.append(int(product_id_str))
            except (ValueError, AttributeError):
                continue  # Skip malformed keys

    return product_ids


def get_recommended_products(request):
    """
    Personalized recommendations based on cart contents
    """
    recommended_ids = set()
    cart_product_ids = get_cart_product_ids(request)

    if not cart_product_ids:
        last_week = timezone.now() - timedelta(days=7)
        trending = CartItem.objects.filter(
            created_at__gte=last_week
        ).values('product_id').annotate(count=Count('product_id')).order_by('-count')[:10]
        return Product.objects.filter(id__in=[t['product_id'] for t in trending])

    # Category-based recommendations from current cart
    categories = Product.objects.filter(id__in=cart_product_ids).values_list('sub_category', flat=True).distinct()
    if categories:
        category_recs = Product.objects.filter(
            sub_category__in=categories
        ).exclude(id__in=cart_product_ids)[:10]
        recommended_ids.update(category_recs.values_list('id', flat=True))

    # Frequently Bought Together
    fbt_recs = get_fbt_recommendations(cart_product_ids)
    recommended_ids.update(fbt_recs.values_list('id', flat=True))

    # Cart co-occurrence (people who added X also added Y)
    related_carts = CartItem.objects.filter(product_id__in=cart_product_ids).values_list('cart_id', flat=True)
    co_added = CartItem.objects.filter(cart_id__in=related_carts)\
        .exclude(product_id__in=cart_product_ids)\
        .values('product_id')\
        .annotate(freq=Count('product_id'))\
        .order_by('-freq')[:10]
    recommended_ids.update([item['product_id'] for item in co_added])

    # Final fallback if still empty
    if not recommended_ids:
        last_week = timezone.now() - timedelta(days=7)
        trending = CartItem.objects.filter(created_at__gte=last_week)\
            .values('product_id').annotate(count=Count('product_id')).order_by('-count')[:10]
        recommended_ids.update([t['product_id'] for t in trending])

    return Product.objects.filter(id__in=list(recommended_ids)[:10]).distinct()


def get_trending_products():
    last_week = timezone.now() - timedelta(days=7)
    trending = CartItem.objects.filter(created_at__gte=last_week)\
        .values('product_id').annotate(count=Count('product_id')).order_by('-count')[:10]
    return Product.objects.filter(id__in=[t['product_id'] for t in trending])


def get_cart_based_recommendations(product_id):
    related_cart_ids = CartItem.objects.filter(product_id=product_id).values_list('cart_id', flat=True)
    related = CartItem.objects.filter(cart_id__in=related_cart_ids)\
        .exclude(product_id=product_id)\
        .values('product_id').annotate(freq=Count('product_id')).order_by('-freq')[:10]
    return Product.objects.filter(id__in=[r['product_id'] for r in related])


def get_fbt_recommendations(cart_product_ids):
    recommended = []
    for product_id in cart_product_ids:
        related = FrequentlyBoughtTogether.objects.filter(
            product_id=product_id
        ).values_list('recommended_id', flat=True)
        recommended.extend(related)
    counter = Counter(recommended)
    top_ids = [item[0] for item in counter.most_common(10)]
    final_ids = [pid for pid in top_ids if pid not in cart_product_ids]
    return Product.objects.filter(id__in=final_ids)

# utils/trending.py

from django.utils import timezone
from datetime import timedelta
from .models import Product
from order.models import CartItem, OrderProduct

def calculate_trending_score(product):
    now = timezone.now()
    recent_days = now - timedelta(days=7)

    # 1. Views (weight = 1)
    views_score = product.views

    # 2. Recent Add to Cart (weight = 2)
    cart_count = CartItem.objects.filter(
        product=product, date__gte=recent_days
    ).count()
    cart_score = 2 * cart_count

    # 3. Recent Purchases (weight = 3)
    order_count = OrderProduct.objects.filter(
        product=product, date_created__gte=recent_days
    ).count()
    order_score = 3 * order_count

    # Combine all
    return views_score + cart_score + order_score

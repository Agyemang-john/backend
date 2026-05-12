from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from order.models import Cart
from django.db.models.signals import post_save, post_delete
from product.models import Product, ProductReview
from django.core.cache import cache
from django.db.models import Avg, Count



@receiver(user_logged_in)
def merge_carts(sender, request, user, **kwargs):
    try:
        device = request.COOKIES['device']
    except KeyError:
        device = None

    if device:
        anonymous_cart_items = Cart.objects.filter(session_id=device)
        for item in anonymous_cart_items:
            cart_item, created = Cart.objects.get_or_create(
                user=user, 
                product=item.product,
                variant=item.variant or None,
                quantity = item.quantity
            )
            if not created:
                cart_item.quantity += item.quantity
                cart_item.save()
            item.delete()
            
@receiver(user_logged_out)
def save_carts_before_logout(sender, request, user, **kwargs):
    try:
        device = request.COOKIES['device']
    except KeyError:
        device = None

    if device:
        anonymous_cart_items = Cart.objects.filter(user=user)
        for item in anonymous_cart_items:
            cart_item, created = Cart.objects.get_or_create(
                session_id=device,
                product=item.product,
                variant=item.variant or None,
                quantity = item.quantity
            )
            if not created:
                cart_item.quantity += item.quantity
                cart_item.save()
            item.delete()

# from product.models import Product
# from elasticsearch8 import Elasticsearch

# es = Elasticsearch(['http://elasticsearch:9200'])

# @receiver(post_save, sender=Product)
# def update_product_index(sender, instance, **kwargs):
#     if instance.status == 'published':
#         doc = {
#             # Your document structure
#         }
#         es.index(index="products", id=instance.id, body=doc)

# @receiver(post_delete, sender=Product)
# def delete_product_index(sender, instance, **kwargs):
#     es.delete(index="products", id=instance.id, ignore=[404])

@receiver([post_save, post_delete], sender=Product)
def invalidate_category_cache(sender, instance, **kwargs):
    cache_key = f"product_detail_cache:{instance.sku}:{instance.slug}"
    cache.delete(cache_key)


@receiver([post_save, post_delete], sender=ProductReview)
def sync_product_rating(sender, instance, **kwargs):
    if not instance.product_id:
        return
    stats = ProductReview.objects.filter(
        product_id=instance.product_id, status=True
    ).aggregate(avg=Avg('rating'), count=Count('id'))
    Product.objects.filter(pk=instance.product_id).update(
        avg_rating=round(stats['avg'] or 0.0, 2),
        review_count=stats['count'] or 0,
    )


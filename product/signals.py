from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from order.models import Cart
from django.db.models.signals import post_save, post_delete, m2m_changed
from product.models import Product, ProductReview, ReviewMedia
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

def product_detail_cache_key(product):
    # Must match product/views.get_cached_product_data.
    return f"product_detail_v2:{product.sku}:{product.slug}"


@receiver([post_save, post_delete], sender=Product)
def invalidate_category_cache(sender, instance, **kwargs):
    cache.delete(product_detail_cache_key(instance))


@receiver(m2m_changed, sender=Product.available_in_regions.through)
def invalidate_on_region_change(sender, instance, action, reverse, **kwargs):
    # The storefront decides "ships to you" from the cached available_in_regions,
    # and regions are saved with .set() after Product.save(), so post_save
    # alone would leave the old list cached.
    if not reverse:
        if action in ('post_add', 'post_remove', 'post_clear'):
            cache.delete(product_detail_cache_key(instance))
        return
    # Edited from the Country side: instance is a Country. A clear() has to be
    # caught before the rows go, while we can still see which products had it.
    if action == 'pre_clear':
        products = Product.objects.filter(available_in_regions=instance)
    elif action in ('post_add', 'post_remove'):
        products = Product.objects.filter(pk__in=kwargs.get('pk_set') or ())
    else:
        return
    cache.delete_many([product_detail_cache_key(p) for p in products.only('sku', 'slug')])


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



# ── Review summary cache & media flag (product/review_views.py) ──────────────

@receiver([post_save, post_delete], sender=ProductReview)
def invalidate_review_summary(sender, instance, **kwargs):
    if instance.product_id:
        from .review_views import summary_cache_key
        cache.delete(summary_cache_key(instance.product_id))


@receiver([post_save, post_delete], sender=ReviewMedia)
def sync_review_media_flag(sender, instance, **kwargs):
    """Keep ProductReview.has_media true only while it has visible media (staff may hide some)."""
    if not instance.review_id:
        return
    from .review_views import summary_cache_key
    visible = ReviewMedia.objects.filter(review_id=instance.review_id, is_hidden=False).exists()
    ProductReview.objects.filter(pk=instance.review_id).exclude(has_media=visible).update(has_media=visible)
    product_id = ProductReview.objects.filter(pk=instance.review_id).values_list('product_id', flat=True).first()
    if product_id:
        cache.delete(summary_cache_key(product_id))

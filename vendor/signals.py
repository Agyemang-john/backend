from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import Vendor, About, OpeningHour
from product.models import Variants
from django.core.files.storage import default_storage
from vendor.cache_utils import invalidate_vendor_cache
from product.models import Product, ProductReview
from django.db.models.signals import m2m_changed
from django.core.cache import cache



@receiver(post_save, sender=Vendor)
def create_vendor_profile(sender, instance, created, **kwargs):
    if created and not hasattr(instance, 'about'):
        About.objects.create(vendor=instance)

from payments.models import *

@receiver(post_save, sender=Subscription)
def update_vendor_subscription(sender, instance, created, **kwargs):
    if created:
        vendor = instance.vendor
        vendor.is_subscribed = True
        vendor.subscription_end_date = instance.end_date  # Assuming Subscription model has 'end_date' field
        vendor.save()

@receiver(post_delete, sender=Variants)
def delete_variant_image(sender, instance, **kwargs):
    if instance.image:
        try:
            default_storage.delete(instance.image.name)
        except Exception as e:
            print(f"Failed to delete image {instance.image.name}: {e}")

@receiver([post_save, post_delete], sender=Vendor)
def invalidate_vendor_cache_on_vendor_change(sender, instance, **kwargs):
    cache_key = f"vendor_metadata:{instance.slug}"
    cache.delete(cache_key)

@receiver([post_save, post_delete], sender=About)
def invalidate_on_about_change(sender, instance, **kwargs):
    if hasattr(instance, 'vendor') and instance.vendor:
        cache_key = f"vendor_metadata:{instance.vendor.slug}"
        cache.delete(cache_key)

@receiver([post_save, post_delete], sender=Product)
def invalidate_vendor_cache_on_product_change(sender, instance, **kwargs):
    cache_key = f"vendor_products:{instance.vendor.slug}"
    cache.delete(cache_key)


@receiver([post_save, post_delete], sender=ProductReview)
def invalidate_vendor_cache_on_review_change(sender, instance, **kwargs):
    cache_key = f"vendor_reviews:{instance.vendor.slug}"
    cache.delete(cache_key)

@receiver([post_save, post_delete], sender=OpeningHour)
def invalidate_vendor_cache_on_opening_hour_change(sender, instance, **kwargs):
    cache_key = f"vendor_metadata:{instance.vendor.slug}"
    cache.delete(cache_key)


@receiver(m2m_changed, sender=Vendor.followers.through)
def invalidate_vendor_cache_on_follow_change(sender, instance, **kwargs):
    action = kwargs.get('action')
    if action in ['post_add', 'post_remove', 'pre_clear', 'post_clear']:
        invalidate_vendor_cache(instance.slug)
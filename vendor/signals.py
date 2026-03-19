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

@receiver(post_save, sender=VendorSubscription)
def sync_vendor_subscription(sender, instance, **kwargs):
    """
    Keep Vendor subscription fields in sync with the latest subscription record.
    Handles active, trial, expired, and cancelled states properly.
    """

    vendor = instance.vendor

    # Determine if subscription is valid
    is_active = instance.is_active()
    is_trial = instance.is_on_trial()

    # Decide overall subscription state
    vendor.is_subscribed = is_active or is_trial

    # Set appropriate end date
    if is_active:
        vendor.subscription_end_date = instance.end_date
    elif is_trial:
        vendor.subscription_end_date = instance.trial_end_date
    else:
        # Only clear if this is the latest subscription
        latest = vendor.subscriptions.order_by('-created_at').first()
        if latest and latest.id == instance.id:
            vendor.subscription_end_date = None
            vendor.is_subscribed = False

    # Save only changed fields (efficient)
    vendor.save(update_fields=["is_subscribed", "subscription_end_date"])

    
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
from django.db.models.signals import post_save, post_delete
from django.conf import settings
from django.dispatch import receiver
from .models import Vendor, About
from userauths.models import User, Profile
from product.models import Variants
from django.core.files.storage import default_storage

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


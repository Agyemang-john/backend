from django.db.models.signals import post_save
from django.conf import settings
from django.dispatch import receiver
from .models import Vendor, About
from userauths.models import User, Profile

@receiver(post_save, sender=Vendor)
def create_vendor_profile(sender, instance, created, **kwargs):
    if created:  # Execute only if the Vendor instance is newly created
        About.objects.get_or_create(vendor=instance)

from payments.models import *

@receiver(post_save, sender=Subscription)
def update_vendor_subscription(sender, instance, created, **kwargs):
    if created:
        vendor = instance.vendor
        vendor.is_subscribed = True
        vendor.subscription_end_date = instance.end_date  # Assuming Subscription model has 'end_date' field
        vendor.save()


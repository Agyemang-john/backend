from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from product.models import *
from django.contrib.auth.signals import user_logged_in
from .tasks import send_order_email_to_sellers, send_order_email_to_customer    
from .models import Order


@receiver(pre_save, sender=ProductDeliveryOption)
def ensure_one_default(sender, instance, **kwargs):
    if instance.default:
        ProductDeliveryOption.objects.filter(product=instance.product, default=True).update(default=False)

@receiver(post_save, sender=Order)
def order_created(sender, instance, created, **kwargs):
    if created:
        # Trigger Celery task instead of WebSocket broadcast
        send_order_email_to_sellers.delay(instance.id)

@receiver(post_save, sender=Order)
def order_created_customer_email(sender, instance, created, **kwargs):
    if created and instance.is_ordered:
        send_order_email_to_customer.delay(instance.id)

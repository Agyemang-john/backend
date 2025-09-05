from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from product.models import *
from django.contrib.auth.signals import user_logged_in
from django.http import JsonResponse
from .models import Cart, CartItem

@receiver(pre_save, sender=ProductDeliveryOption)
def ensure_one_default(sender, instance, **kwargs):
    if instance.default:
        ProductDeliveryOption.objects.filter(product=instance.product, default=True).update(default=False)
    
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .models import Order

@receiver(post_save, sender=Order)
def order_created(sender, instance, created, **kwargs):
    if created:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            'orders',
            {
                'type': 'order_message',
                'message': f'New order: {instance.id} for {instance.order_number}',
            }
        )


"""Create the parcel when a pickup order is placed; mark it ready when the order reaches the station."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from order.models import Order


@receiver(post_save, sender=Order, dispatch_uid='pickup_order_saved')
def order_saved(sender, instance, created, **kwargs):
    if kwargs.get('raw') or not instance.is_ordered or not instance.address_id:
        return
    from address.models import Address
    if created:
        if Address.objects.filter(pk=instance.address_id, pickup_point__isnull=False).exists():
            from .services import create_parcel
            order_id = instance.pk
            transaction.on_commit(lambda: create_parcel(Order.objects.select_related('address__pickup_point').get(pk=order_id)))
        return
    if instance.status == 'delivered':
        from .models import PickupParcel
        from .services import mark_ready
        parcel = PickupParcel.objects.filter(order=instance, status=PickupParcel.AWAITING).select_related('point', 'order').first()
        if parcel is not None:
            transaction.on_commit(lambda: mark_ready(parcel))

"""Score each order shortly after it's placed (its lines are written after the Order row)."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from order.models import Order


@receiver(post_save, sender=Order, dispatch_uid='risk_assess_order')
def order_saved(sender, instance, created, **kwargs):
    if kwargs.get('raw') or not created or not instance.is_ordered:
        return
    from .tasks import assess_order
    order_id = instance.pk
    transaction.on_commit(lambda: assess_order.apply_async((order_id,), countdown=5))

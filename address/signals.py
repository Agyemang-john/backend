"""Keep `last_used_at` current so the book lists recent addresses first."""

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='order.Order', dispatch_uid='address_mark_used')
def mark_address_used(sender, instance, created, raw=False, **kwargs):
    if raw or not created or not instance.is_ordered or not instance.address_id:
        return
    from .services import mark_used
    mark_used(instance.address_id)

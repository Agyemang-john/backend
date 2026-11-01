"""Purchases are recorded server-side (can't be blocked or faked by the browser)."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from order.models import Order


@receiver(post_save, sender=Order, dispatch_uid='analytics_purchase')
def order_placed(sender, instance, created, **kwargs):
    if kwargs.get('raw') or not created or not instance.is_ordered or not instance.user_id:
        return
    user_id = instance.user_id

    def _record():
        from django.core.cache import cache

        from .collector import mark_converted, record, visitor_experiments
        visitor = cache.get(f"an:uv:{user_id}") or f"u:{user_id}"
        record('purchase', visitor, visitor_experiments(visitor))
        mark_converted(user_id)

    transaction.on_commit(_record)

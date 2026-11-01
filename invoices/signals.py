"""Issue the receipt and seller invoices shortly after an order is placed (lines are written after the Order row)."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from order.models import Order


@receiver(post_save, sender=Order, dispatch_uid='invoices_issue_on_order')
def order_saved(sender, instance, created, **kwargs):
    if kwargs.get('raw') or not instance.is_ordered:
        return
    if not created and 'is_ordered' not in (kwargs.get('update_fields') or ()) and instance.invoices.exists():
        return
    from .tasks import issue_order_invoices
    order_id = instance.pk
    transaction.on_commit(lambda: issue_order_invoices.apply_async((order_id,), countdown=60))

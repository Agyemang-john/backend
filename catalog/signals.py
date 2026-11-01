"""Keep groups and featured offers current as listings change."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from product.models import Product
from vendor.models import Vendor


@receiver(post_save, sender=Product, dispatch_uid='catalog_product_saved')
def product_saved(sender, instance, created, **kwargs):
    if kwargs.get('raw'):
        return
    from .services import attach_by_gtin
    if instance.gtin:
        attach_by_gtin(instance)
    if instance.catalog_item_id:
        from .buybox import recompute
        item_id = instance.catalog_item_id
        transaction.on_commit(lambda: recompute([item_id]))


@receiver(post_save, sender=Vendor, dispatch_uid='catalog_vendor_saved')
def vendor_saved(sender, instance, created, **kwargs):
    """Pausing/suspending a store hands its featured offers to the next store."""
    if created or kwargs.get('raw'):
        return
    item_ids = list(Product.objects.filter(vendor=instance, catalog_item__isnull=False)
                    .values_list('catalog_item_id', flat=True).distinct()[:5000])
    if item_ids:
        from .tasks import recompute_items
        transaction.on_commit(lambda: recompute_items.delay(item_ids))

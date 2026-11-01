"""Wishlisting a product watches its price; un-wishlisting stops it (unless the shopper asked separately)."""
import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from order.models import CartItem
from product.models import Wishlist

from .models import ProductAlert

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Wishlist, dispatch_uid='engagement_wishlist_watch')
def wishlist_added(sender, instance, created, **kwargs):
    if not created or kwargs.get('raw'):
        return
    user_id, product_id = instance.user_id, instance.product_id

    def _watch():
        from .alerts import watch
        try:
            alert = ProductAlert.objects.filter(user_id=user_id, product_id=product_id,
                                                variant__isnull=True, kind=ProductAlert.PRICE).first()
            if alert is not None and alert.is_active:
                return
            watch(instance.user, instance.product, ProductAlert.PRICE, source=ProductAlert.WISHLIST)
        except Exception:  # noqa: BLE001 - never break wishlisting over an alert
            logger.exception("could not create wishlist price alert for user %s", user_id)

    transaction.on_commit(_watch)


@receiver(post_delete, sender=Wishlist, dispatch_uid='engagement_wishlist_unwatch')
def wishlist_removed(sender, instance, **kwargs):
    ProductAlert.objects.filter(
        user_id=instance.user_id, product_id=instance.product_id, variant__isnull=True,
        kind=ProductAlert.PRICE, source=ProductAlert.WISHLIST,
    ).delete()


@receiver(post_save, sender=CartItem, dispatch_uid='engagement_cart_touch_save')
@receiver(post_delete, sender=CartItem, dispatch_uid='engagement_cart_touch_delete')
def cart_changed(sender, instance, **kwargs):
    """Adding, changing or removing a line makes the cart "active" again for cart reminders."""
    if kwargs.get('raw'):
        return
    from .cart_reminders import touch_cart
    touch_cart(instance.cart_id)

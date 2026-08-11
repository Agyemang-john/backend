"""
recommendation/signals.py

Cache invalidation only — no training happens here.

The deal cache is the one piece of recommendation state that goes stale on a
timescale shorter than the hourly scoring run: a seller changes a price or
launches a flash sale and expects to see it immediately. Everything else
(neighbours, embeddings, rails) is versioned by model run and changes only when
a run completes.
"""

import logging

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _invalidate_deals():
    from .serving import bump_deals_version

    cache.delete('deals_products')          # legacy core.DealsAPIView cache
    bump_deals_version()


@receiver(post_save, sender='product.FlashSale')
@receiver(post_delete, sender='product.FlashSale')
def flash_sale_changed(sender, instance, **kwargs):
    """A flash sale starting or ending changes the deals page immediately."""
    _invalidate_deals()


@receiver(post_save, sender='recommendation.ProductDealScore')
def deal_score_written(sender, instance, created, **kwargs):
    """
    Bulk rescoring writes thousands of rows; only bust the cache for the
    single-row edits an admin makes by hand. `bulk_create` does not fire
    post_save, so the hourly task is unaffected by this.
    """
    if not created:
        _invalidate_deals()

"""
Choosing the featured offer ("buy box") of each CatalogItem.

Score = price, made worse by things that make an offer a poorer buy:
  - slow dispatch (store handling_days beyond 1)       +1.5% per day
  - poorly rated listing (3+ reviews averaging < 3.5)   +8%
  - imported / made-to-order (longer wait)              +10%
Only offers that can be bought right now compete (published, store open,
in stock). If none can, the item still shows once, out of stock.

recompute(item_ids) is set-based: a few queries per call whatever the
number of offers, and it writes only rows whose flag actually changes.
"""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum

HANDLING_PENALTY = Decimal('0.015')
LOW_RATING_PENALTY = Decimal('0.08')
SOURCING_PENALTY = Decimal('0.10')


def _score(p, stock):
    from product.sourcing import IN_STOCK
    factor = Decimal('1')
    handling = getattr(p.vendor, 'handling_days', 2) or 0
    if handling > 1:
        factor += HANDLING_PENALTY * (handling - 1)
    if p.review_count >= 3 and p.avg_rating < 3.5:
        factor += LOW_RATING_PENALTY
    if p.fulfilment_type != IN_STOCK:
        factor += SOURCING_PENALTY
    return p.price * factor


def _eligible(p, stock):
    v = p.vendor
    return (p.status == 'published' and v is not None and v.is_approved and not v.shop_paused
            and not v.is_suspended and stock > 0)


def recompute(item_ids):
    """Refresh winners and the denormalised counts for these CatalogItems."""
    from product.models import CatalogItem, Product, Variants

    item_ids = list(set(item_ids))
    if not item_ids:
        return 0
    offers = list(Product.objects.filter(catalog_item_id__in=item_ids).select_related('vendor')
                  .only('pk', 'catalog_item_id', 'price', 'status', 'variant', 'total_quantity', 'fulfilment_type',
                        'avg_rating', 'review_count', 'date', 'is_buybox_winner',
                        'vendor__is_approved', 'vendor__shop_paused', 'vendor__is_suspended', 'vendor__handling_days'))
    variant_stock = dict(Variants.objects.filter(product__catalog_item_id__in=item_ids)
                         .values_list('product_id').annotate(total=Sum('quantity')))

    by_item = defaultdict(list)
    for p in offers:
        stock = (variant_stock.get(p.pk, 0) if p.variant != 'None' else p.total_quantity) or 0
        by_item[p.catalog_item_id].append((p, stock))

    winners, item_updates = set(), []
    for item_id in item_ids:
        group = by_item.get(item_id, [])
        live = [(p, s) for p, s in group if _eligible(p, s)]
        if live:
            winner = min(live, key=lambda ps: (_score(*ps), ps[0].date))[0]
        else:
            published = [p for p, _ in group if p.status == 'published']
            winner = min(published, key=lambda p: p.date) if published else None
        if winner is not None:
            winners.add(winner.pk)
        prices = [p.price for p, _ in live] or [p.price for p, _ in group if p.status == 'published']
        item_updates.append(CatalogItem(pk=item_id, winner_id=winner.pk if winner else None,
                                        offer_count=len([1 for p, _ in group if p.status == 'published']),
                                        lowest_price=min(prices) if prices else None))

    flips = []
    for p, _ in (ps for group in by_item.values() for ps in group):
        should = p.pk in winners
        if p.is_buybox_winner != should:
            p.is_buybox_winner = should
            flips.append(p)
    if flips:
        Product.objects.bulk_update(flips, ['is_buybox_winner'], batch_size=500)
    CatalogItem.objects.bulk_update(item_updates, ['winner_id', 'offer_count', 'lowest_price'], batch_size=500)
    return len(flips)


def recompute_all(chunk=500):
    """Periodic safety net: stock changes by F() updates don't fire signals."""
    from product.models import CatalogItem
    ids = list(CatalogItem.objects.values_list('pk', flat=True).order_by('pk'))
    changed = 0
    for i in range(0, len(ids), chunk):
        changed += recompute(ids[i:i + chunk])
    return changed

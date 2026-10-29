"""
product/sourcing.py
Items a seller does not hold in stock: imported once ordered ("ships from
abroad") or made to order. The seller sets how long getting the item takes;
that time comes before the courier's days wherever a delivery date is shown
or promised.

A variant may override its product (black drone in stock, white one imported):
a blank Variants.fulfilment_type inherits the product's settings.
"""

IN_STOCK = 'in_stock'
IMPORTED = 'imported'
MADE_TO_ORDER = 'made_to_order'
FULFILMENT_CHOICES = [
    (IN_STOCK, 'In stock'),
    (IMPORTED, 'Ships from abroad'),
    (MADE_TO_ORDER, 'Made to order'),
]

# Ten weeks: longer than that is a pre-order, not a listing.
MAX_SOURCING_DAYS = 70


def _source(product, variant=None):
    if variant is not None and getattr(variant, 'fulfilment_type', ''):
        return variant
    return product


def fulfilment_type(product, variant=None):
    return _source(product, variant).fulfilment_type or IN_STOCK


def window(product, variant=None):
    """(min_days, max_days) needed to get the item before it can ship; (0, 0) in stock."""
    if product is None:
        return 0, 0
    src = _source(product, variant)
    if (src.fulfilment_type or IN_STOCK) == IN_STOCK:
        return 0, 0
    return src.sourcing_min_days or 0, src.sourcing_max_days or 0


def info(product, variant=None, with_origin=True):
    """What the storefront shows, or None for an in-stock item."""
    kind = fulfilment_type(product, variant)
    if kind == IN_STOCK:
        return None
    low, high = window(product, variant)
    data = {
        'type': kind,
        'label': dict(FULFILMENT_CHOICES)[kind],
        'min_days': low,
        'max_days': high,
    }
    if with_origin:
        # sourced_from_id avoids a query when the product has no origin set.
        origin = product.sourced_from if product.sourced_from_id else None
        data['from'] = origin.name if origin else None
    return data


def validate(kind, low, high):
    """Error message for a seller's settings, or None when they are fine."""
    if kind not in dict(FULFILMENT_CHOICES):
        return "Choose how this item is fulfilled."
    if kind == IN_STOCK:
        return None
    if not low or not high:
        return "Say how long it takes to get this item."
    if low > high:
        return "The shortest time can't be longer than the longest."
    if high > MAX_SOURCING_DAYS:
        return f"The longest time can be at most {MAX_SOURCING_DAYS // 7} weeks."
    return None


def filter_fast_delivery(qs, request):
    """Listing filter ?fast=1: only items the seller already holds."""
    if request.GET.get('fast') == '1':
        return qs.filter(fulfilment_type=IN_STOCK)
    return qs

"""
Search, filters and sort for the seller dashboard product grid
(GET /api/v1/vendor/products/ → ProductListCreateView).

Query params (all optional, all combinable):
  search      words matched against title, seller SKU, Negromart SKU and brand;
              every word must match (so "red dress" finds "Red Midi Dress").
              A full GTIN/barcode also matches exactly.
  status      product status (published, in_review, draft, disabled, rejected)
  stock       in | low | out   (low = 1..vendor.low_stock_threshold)
  category    sub-category id
  fulfilment  in_stock | imported | made_to_order
  min_price / max_price   GHS, inclusive
  sort        one of SORTS below (default newest)
"""
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Q

from product.sourcing import FULFILMENT_CHOICES

SORTS = {
    'newest':     ('-date', '-id'),
    'oldest':     ('date', 'id'),
    'updated':    ('-updated', '-date'),
    'title':      ('title', 'id'),
    'price_low':  ('price', 'id'),
    'price_high': ('-price', '-id'),
    'stock_low':  ('total_quantity', 'id'),
    'stock_high': ('-total_quantity', '-id'),
    'most_viewed': ('-views', '-id'),
}
DEFAULT_SORT = 'newest'


def _decimal(value):
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d >= 0 else None


def search_q(text):
    """Q for a free-text product search; None when there's nothing to search."""
    words = [w for w in (text or '').split() if w][:8]  # cap: one query, not a DoS
    if not words:
        return None
    q = Q()
    for w in words:
        q &= (Q(title__icontains=w) | Q(seller_sku__icontains=w)
              | Q(sku__icontains=w) | Q(brand__title__icontains=w))
    whole = ' '.join(words)
    return q | Q(gtin=whole)


def stock_q(kind, threshold):
    if kind == 'out':
        return Q(total_quantity=0)
    if kind == 'low':
        return Q(total_quantity__gt=0, total_quantity__lte=threshold)
    if kind == 'in':
        return Q(total_quantity__gt=0)
    return None


def apply_filters(qs, params, vendor):
    """Narrow a vendor's Product queryset by the dashboard's query params."""
    q = search_q(params.get('search', ''))
    if q is not None:
        qs = qs.filter(q)

    status_f = params.get('status', '').strip()
    if status_f and status_f != 'all':
        qs = qs.filter(status=status_f)

    sq = stock_q(params.get('stock', '').strip(), vendor.low_stock_threshold)
    if sq is not None:
        qs = qs.filter(sq)

    category = params.get('category', '').strip()
    if category.isdigit():
        qs = qs.filter(sub_category_id=int(category))

    fulfilment = params.get('fulfilment', '').strip()
    if fulfilment in dict(FULFILMENT_CHOICES):
        qs = qs.filter(fulfilment_type=fulfilment)

    lo, hi = _decimal(params.get('min_price', '')), _decimal(params.get('max_price', ''))
    if lo is not None:
        qs = qs.filter(price__gte=lo)
    if hi is not None:
        qs = qs.filter(price__lte=hi)

    return qs.order_by(*SORTS.get(params.get('sort', ''), SORTS[DEFAULT_SORT]))


def facets(vendor_qs, vendor):
    """
    Catalog-wide counts for the filter menus (ignore the current filters, so a
    menu never hides the option you'd switch to).
    """
    categories = (
        vendor_qs.filter(sub_category__isnull=False)
        .values('sub_category_id', 'sub_category__title')
        .annotate(c=Count('id'))
        .order_by('sub_category__title')
    )
    threshold = vendor.low_stock_threshold
    stock = vendor_qs.aggregate(
        in_stock=Count('id', filter=Q(total_quantity__gt=0)),
        low=Count('id', filter=Q(total_quantity__gt=0, total_quantity__lte=threshold)),
        out=Count('id', filter=Q(total_quantity=0)),
    )
    return {
        'categories': [
            {'id': row['sub_category_id'], 'title': row['sub_category__title'], 'count': row['c']}
            for row in categories
        ],
        'stock_counts': {'in': stock['in_stock'], 'low': stock['low'], 'out': stock['out']},
        'low_stock_threshold': threshold,
    }

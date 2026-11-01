"""
The extra facts a storefront product card shows under the price, Walmart-style:

    colour swatches (first few + "+N"), "Sizes XS-3XL" / "12 options",
    "arrives tomorrow" (fastest delivery option, plus sourcing days),
    and one badge: the live flash-sale label ("Clearance", "Lightning Deal"),
    "Best seller", "Only 3 left" or "New".

Shared by every listing (category, brand, search, collection, deals, seller
storefront, sponsored rows) so cards look the same everywhere.

Speed: one call per page, never per product. Cache hits cost one get_many; a
miss costs three small queries for the missing products (variants, delivery
options, live flash sales), and the result is cached per product for a few
minutes, so a busy listing serves cards from cache.
"""
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

CACHE_TTL = 300
CACHE_PREFIX = 'card:v1:'
MAX_SWATCHES = 6
LOW_STOCK = 5
NEW_DAYS = 14

# Apparel sizes in wearing order, so a range reads "XS-3XL" rather than "3XL-XS".
_SIZE_ORDER = ['XXXS', '3XS', 'XXS', '2XS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', '2XL', 'XXXL', '3XL',
               'XXXXL', '4XL', '5XL', '6XL']
_SIZE_RANK = {s: i for i, s in enumerate(_SIZE_ORDER)}


def _size_label(names):
    """'Sizes XS-3XL', 'Sizes 38-44', or '5 sizes' when the sizes don't sort."""
    names = [n.strip() for n in dict.fromkeys(names) if n and n.strip()]
    if not names:
        return ''
    if len(names) == 1:
        return f"Size {names[0]}"
    upper = [n.upper() for n in names]
    if all(n in _SIZE_RANK for n in upper):
        ordered = sorted(upper, key=_SIZE_RANK.get)
        return f"Sizes {ordered[0]}-{ordered[-1]}"
    try:
        nums = sorted(float(n) for n in names)
        fmt = lambda x: f"{x:g}"   # noqa: E731
        return f"Sizes {fmt(nums[0])}-{fmt(nums[-1])}"
    except ValueError:
        return f"{len(names)} sizes"


def _build(ids):
    """{product_id: card} for these product ids, straight from the database."""
    from .models import FlashSale, Product, ProductDeliveryOption, Variants

    # Read our own columns: callers often load products with .only(), and touching a
    # deferred column would cost a query per product.
    products = list(Product.objects.filter(pk__in=ids).only('pk', *CARD_FIELDS))
    swatches, sizes, option_count = defaultdict(dict), defaultdict(list), defaultdict(int)
    for pid, vid, cname, ccode, sname in (Variants.objects.filter(product_id__in=ids)
                                          .order_by('product_id', 'id')
                                          .values_list('product_id', 'id', 'color__name', 'color__code', 'size__name')):
        option_count[pid] += 1
        if cname and cname not in swatches[pid]:
            swatches[pid][cname] = {'variant_id': vid, 'name': cname, 'code': ccode or ''}
        if sname:
            sizes[pid].append(sname)

    fastest = {}
    for pid, lo, hi in (ProductDeliveryOption.objects.filter(product_id__in=ids, variant__isnull=True)
                        .values_list('product_id', 'delivery_option__min_days', 'delivery_option__max_days')):
        if pid not in fastest or (hi, lo) < (fastest[pid][1], fastest[pid][0]):
            fastest[pid] = (lo or 0, hi or 0)

    now = timezone.now()
    flash = {}
    for pid, label, end in (FlashSale.objects.filter(product_id__in=ids, is_active=True, start_time__lte=now,
                                                     end_time__gte=now)
                            .order_by('end_time').values_list('product_id', 'label', 'end_time')):
        flash.setdefault(pid, (label, end))
    labels = dict(FlashSale.LABEL_CHOICES)

    out = {}
    for p in products:
        colors = list(swatches[p.pk].values())
        has_variants = (p.variant or 'None') != 'None' and option_count[p.pk] > 0
        delivery = None
        if p.pk in fastest:
            lo, hi = fastest[p.pk]
            src_lo, src_hi = (p.sourcing_min_days or 0, p.sourcing_max_days or 0) \
                if (p.fulfilment_type or 'in_stock') != 'in_stock' else (0, 0)
            delivery = {'min_days': lo + src_lo, 'max_days': hi + src_hi}

        badge = None
        if p.pk in flash:
            label, end = flash[p.pk]
            badge = {'kind': 'flash', 'label': labels.get(label, 'Deal'), 'ends_at': end.isoformat()}
        elif p.popular_product:
            badge = {'kind': 'best_seller', 'label': 'Best seller'}
        elif not has_variants and p.total_quantity is not None and 0 < p.total_quantity <= LOW_STOCK:
            badge = {'kind': 'low_stock', 'label': f"Only {p.total_quantity} left"}
        elif p.date and p.date >= now - timedelta(days=NEW_DAYS):
            badge = {'kind': 'new', 'label': 'New'}

        out[p.pk] = {
            'colors': colors[:MAX_SWATCHES],
            'color_count': len(colors),
            'size_label': _size_label(sizes[p.pk]) if has_variants else '',
            'options': option_count[p.pk] if has_variants else 0,
            'delivery': delivery,
            'badge': badge,
        }
    return out


def card_extras(products):
    """{product_id: card} for a page of products (instances or ids), cached per product."""
    ids = list(dict.fromkeys(getattr(p, 'pk', p) for p in products if p is not None))
    if not ids:
        return {}
    keys = {pid: f"{CACHE_PREFIX}{pid}" for pid in ids}
    try:
        hit = cache.get_many(list(keys.values()))
    except Exception:  # noqa: BLE001 - cache down: build everything
        hit = {}
    out = {pid: hit[key] for pid, key in keys.items() if key in hit}
    missing = [pid for pid in ids if pid not in out]
    if missing:
        built = _build(missing)
        out.update(built)
        try:
            cache.set_many({keys[pid]: card for pid, card in built.items()}, CACHE_TTL)
        except Exception:  # noqa: BLE001
            pass
    return out


def forget(product_id):
    """Drop a product's cached card (after its variants, stock or delivery change)."""
    cache.delete(f"{CACHE_PREFIX}{product_id}")


# Product columns a card reads (loaded by _build itself).
CARD_FIELDS = ('variant', 'popular_product', 'total_quantity', 'date',
               'fulfilment_type', 'sourcing_min_days', 'sourcing_max_days')


def listing_rows(products, request):
    """
    Storefront grid rows: {product, average_rating, review_count, colors, card}.
    `colors` keeps the old swatch shape for older clients; `card` is the new
    card detail from card_extras.
    """
    from .serializers import ProductListSerializer

    products = list(products)
    extras = card_extras(products)
    context = {'request': request}      # shared: the serializer memoises the exchange rate in it
    rows = []
    for p in products:
        card = extras.get(p.pk) or {}
        rows.append({
            'product': ProductListSerializer(p, context=context).data,
            'average_rating': p.avg_rating,
            'review_count': p.review_count,
            'colors': [{'id': c['variant_id'], 'color__name': c['name'], 'color__code': c['code']}
                       for c in card.get('colors', [])],
            'card': card,
        })
    return rows

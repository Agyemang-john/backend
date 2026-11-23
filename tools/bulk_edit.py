"""
tools/bulk_edit.py
Change price, old price, stock and cost of many products and versions at once.

Three ways in, one way to save (apply_changes):
  - edits typed into the Seller Centre table (a list of row changes),
  - an action on a selection ("10% off", "add 20 to stock"),
  - a CSV exported from here and edited in Excel/Sheets.

Saving is one transaction with bulk_update (a handful of queries for any
number of rows). bulk_update skips Model.save(), so what save() and its
signals would have done is done here: the product page cache is cleared and
multi-seller buy boxes are recomputed for the items touched.
"""
import csv
import io
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.cache import cache
from django.db import transaction
from django.db.models import Prefetch, Q

from product.models import Product, Variants
from product.signals import product_detail_cache_key
from product.variations import effective_axes, option_label

from .models import PriceChange

MAX_CHANGES = 2000
PAGE_SIZE = 50
MAX_PRICE = Decimal('99999999')
CSV_HEADERS = ['product_id', 'variant_id', 'title', 'version', 'seller_sku', 'price', 'compare_at_price',
               'stock', 'unit_cost']
FIELDS = ('price', 'compare_at_price', 'stock', 'unit_cost')


class BulkEditError(ValueError):
    pass


def _money(value, field, allow_empty=False):
    if value in (None, ''):
        if allow_empty:
            return None
        raise BulkEditError(f'{field}: required.')
    try:
        amount = Decimal(str(value).replace(',', '').replace('GHS', '').replace('₵', '').strip())
    except InvalidOperation:
        raise BulkEditError(f'{field}: "{value}" is not a number.')
    if amount < 0 or amount > MAX_PRICE:
        raise BulkEditError(f'{field}: must be 0 or more.')
    return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _stock(value):
    try:
        number = int(Decimal(str(value).replace(',', '').strip()))
    except (InvalidOperation, ValueError):
        raise BulkEditError(f'stock: "{value}" is not a whole number.')
    if number < 0:
        raise BulkEditError('stock: can\'t be negative.')
    return number


# ── Reading ──────────────────────────────────────────────────────────────────

def _queryset(vendor, search='', status=''):
    qs = Product.objects.filter(vendor=vendor)
    if status:
        qs = qs.filter(status=status)
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(seller_sku__iexact=search) | Q(sku__iexact=search)
                       | Q(variants__seller_sku__iexact=search)).distinct()
    return qs


def _row(product, variant=None, axes=None):
    if variant is None:
        return {
            'product_id': product.pk, 'variant_id': None, 'title': product.title, 'version': '',
            'seller_sku': product.seller_sku, 'price': str(product.price),
            'compare_at_price': str(product.old_price or '0.00'), 'stock': product.total_quantity or 0,
            'unit_cost': str(product.unit_cost) if product.unit_cost is not None else '',
            'status': product.status, 'image': product.image.url if product.image else None,
        }
    return {
        'product_id': product.pk, 'variant_id': variant.pk, 'title': product.title,
        'version': option_label(variant, axes), 'seller_sku': variant.seller_sku, 'price': str(variant.price),
        'compare_at_price': None, 'stock': variant.quantity,
        'unit_cost': str(variant.unit_cost) if variant.unit_cost is not None else '',
        'status': product.status, 'image': variant.image.url if variant.image and variant.image.name != 'product.jpg' else None,
    }


def rows_for(products):
    """Products → editable rows: one per simple product, one per version otherwise."""
    out = []
    for p in products:
        versions = list(p.variants.all()) if p.variant != 'None' else []
        if versions:
            axes = effective_axes(p)
            out.extend(_row(p, v, axes) for v in versions)
        else:
            out.append(_row(p))
    return out


def page(vendor, search='', status='', number=1):
    qs = _queryset(vendor, search, status).order_by('-updated', '-pk')
    total = qs.count()
    start = (max(number, 1) - 1) * PAGE_SIZE
    products = list(qs.prefetch_related(Prefetch('variants', queryset=Variants.objects.order_by('pk')))[start:start + PAGE_SIZE])
    return {'count': total, 'page': max(number, 1), 'page_size': PAGE_SIZE, 'results': rows_for(products)}


# ── Saving ───────────────────────────────────────────────────────────────────

def _after_save(product_ids):
    """What Product.save() and its signals would have done."""
    products = list(Product.objects.filter(pk__in=product_ids).only('pk', 'sku', 'slug', 'catalog_item_id'))
    cache.delete_many([product_detail_cache_key(p) for p in products])
    items = {p.catalog_item_id for p in products if p.catalog_item_id}
    if items:
        from catalog.buybox import recompute
        transaction.on_commit(lambda: recompute(items))


def apply_changes(vendor, changes, source=PriceChange.BULK_EDIT):
    """
    changes: [{product_id, variant_id (or None), price?, compare_at_price?, stock?, unit_cost?}]
    Rows that don't belong to the store are reported, not saved. Returns
    {'updated': n, 'errors': [{'index', 'product_id', 'variant_id', 'message'}]}.
    All valid rows are saved together; an invalid row never stops the others.
    """
    if len(changes) > MAX_CHANGES:
        raise BulkEditError(f'Up to {MAX_CHANGES} rows at a time.')
    errors = []
    product_ids = {c.get('product_id') for c in changes if not c.get('variant_id')}
    variant_ids = {c.get('variant_id') for c in changes if c.get('variant_id')}

    with transaction.atomic():
        products = {p.pk: p for p in Product.objects.select_for_update()
                    .filter(vendor=vendor, pk__in=[i for i in product_ids if i])}
        variants = {v.pk: v for v in Variants.objects.select_for_update()
                    .filter(product__vendor=vendor, pk__in=variant_ids)}
        dirty_products, dirty_variants, price_log = {}, {}, []

        for index, change in enumerate(changes):
            vid = change.get('variant_id')
            target = variants.get(vid) if vid else products.get(change.get('product_id'))
            if target is None:
                errors.append({'index': index, 'product_id': change.get('product_id'), 'variant_id': vid,
                               'message': 'Not found in your store.'})
                continue
            try:
                updates = {}
                if 'price' in change:
                    price = _money(change['price'], 'price')
                    if price <= 0:
                        raise BulkEditError('price: must be more than 0.')
                    updates['price'] = price
                if 'stock' in change:
                    updates['quantity' if vid else 'total_quantity'] = _stock(change['stock'])
                if 'unit_cost' in change:
                    updates['unit_cost'] = _money(change['unit_cost'], 'unit_cost', allow_empty=True)
                if 'compare_at_price' in change and not vid:
                    updates['old_price'] = _money(change['compare_at_price'], 'compare_at_price', allow_empty=True) or Decimal('0')
                if not vid and 'old_price' in updates:     # only when the seller changes it
                    old_price = updates['old_price']
                    price = updates.get('price', target.price)
                    if old_price and old_price <= price:
                        raise BulkEditError('compare_at_price: must be higher than the price (or 0 for no discount).')
            except BulkEditError as exc:
                errors.append({'index': index, 'product_id': change.get('product_id'), 'variant_id': vid,
                               'message': str(exc)})
                continue

            if 'price' in updates and updates['price'] != target.price:
                price_log.append(PriceChange(
                    vendor=vendor, product_id=target.product_id if vid else target.pk, variant=target if vid else None,
                    old_price=target.price, new_price=updates['price'], source=source))
            for field, value in updates.items():
                setattr(target, field, value)
            if updates:
                (dirty_variants if vid else dirty_products)[target.pk] = target

        if dirty_products:
            Product.objects.bulk_update(list(dirty_products.values()),
                                        ['price', 'old_price', 'total_quantity', 'unit_cost'], batch_size=500)
        if dirty_variants:
            Variants.objects.bulk_update(list(dirty_variants.values()), ['price', 'quantity', 'unit_cost'],
                                         batch_size=500)
        if price_log:
            PriceChange.objects.bulk_create(price_log, batch_size=500)
        touched = set(dirty_products) | {v.product_id for v in dirty_variants.values()}
        if touched:
            _after_save(touched)

    return {'updated': len(dirty_products) + len(dirty_variants), 'errors': errors}


# ── Actions on a selection ───────────────────────────────────────────────────

ACTIONS = {
    'price_percent': 'Change price by %',      # value: -10 = 10% off, 5 = 5% up
    'price_amount': 'Change price by amount',  # value: -5 / +5 GHS
    'price_set': 'Set price',
    'stock_set': 'Set stock',
    'stock_add': 'Add to stock',               # negative removes, never below 0
    'cost_set': 'Set cost price',
}


def action_changes(vendor, op, value, product_ids=None, search='', status=''):
    """Turn an action on a selection (or on everything the filter matches) into row changes."""
    if op not in ACTIONS:
        raise BulkEditError('Unknown action.')
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise BulkEditError('Enter a number.')
    qs = _queryset(vendor, search, status)
    if product_ids is not None:
        qs = qs.filter(pk__in=product_ids)
    products = list(qs.prefetch_related('variants')[:MAX_CHANGES])
    changes = []
    for p in products:
        targets = [(p.pk, v.pk, v.price, v.quantity) for v in p.variants.all()] if p.variant != 'None' else []
        targets = targets or [(p.pk, None, p.price, p.total_quantity or 0)]
        for pid, vid, price, stock in targets:
            change = {'product_id': pid, 'variant_id': vid}
            if op == 'price_percent':
                change['price'] = (price * (Decimal('1') + amount / 100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            elif op == 'price_amount':
                change['price'] = price + amount
            elif op == 'price_set':
                change['price'] = amount
            elif op == 'stock_set':
                change['stock'] = int(amount)
            elif op == 'stock_add':
                change['stock'] = max(stock + int(amount), 0)
            elif op == 'cost_set':
                change['unit_cost'] = amount
            changes.append(change)
    return changes


# ── CSV ──────────────────────────────────────────────────────────────────────

def export_csv(vendor, search='', status=''):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_HEADERS, extrasaction='ignore')
    writer.writeheader()
    qs = _queryset(vendor, search, status).order_by('title').prefetch_related(
        Prefetch('variants', queryset=Variants.objects.order_by('pk')))
    for start in range(0, qs.count(), 500):
        for row in rows_for(qs[start:start + 500]):
            row = {**row, 'variant_id': row['variant_id'] or '', 'compare_at_price': row['compare_at_price'] or ''}
            writer.writerow(row)
    return '﻿' + out.getvalue()


def changes_from_csv(text):
    """Rows of an edited export → changes. Only the editable columns are read; empty cells are left alone."""
    head = text[:2048]
    delimiter = ';' if head.count(';') > head.count(',') else ','
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿')), delimiter=delimiter)
    if not reader.fieldnames or 'product_id' not in [h.strip().lower() for h in reader.fieldnames]:
        raise BulkEditError('Use the file exported from this page (it needs the product_id column).')
    changes = []
    for row in reader:
        row = {(k or '').strip().lower(): (v or '').strip() for k, v in row.items()}
        if not row.get('product_id', '').isdigit():
            continue
        change = {'product_id': int(row['product_id']),
                  'variant_id': int(row['variant_id']) if row.get('variant_id', '').isdigit() else None}
        for field in FIELDS:
            if row.get(field, '') != '':
                change[field] = row[field]
        if len(change) > 2:
            changes.append(change)
        if len(changes) > MAX_CHANGES:
            raise BulkEditError(f'Up to {MAX_CHANGES} rows per file.')
    return changes

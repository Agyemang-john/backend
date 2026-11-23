"""
tools/seller_api.py
The seller API: a store's own systems (a POS, a warehouse sheet, another shop)
read its products and orders and keep stock and prices in sync.

  keys      "nm_<prefix>_<secret>", shown once; only a SHA-256 hash is stored.
            Up to MAX_KEYS live keys per store, read-only or read-write,
            revocable. A key works only while the plan includes the API.
  auth      Authorization: Bearer nm_... on /api/v1/tools/api/* only (the key
            is never accepted anywhere else in the platform).
  limits    RATE per key (DRF throttle), MAX_STOCK_ITEMS rows per stock call.
  writes    go through tools.bulk_edit.apply_changes, so they are checked,
            logged (PriceChange source "api") and clear caches like bulk edit.
"""
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle

from .models import ApiKey, PriceChange

MAX_KEYS = 5
MAX_STOCK_ITEMS = 500
PRODUCTS_PAGE = 100
ORDERS_PAGE = 50
TOUCH_EVERY = timedelta(minutes=5)


class SellerApiError(ValueError):
    """Message is safe to show to the caller."""


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Keys ─────────────────────────────────────────────────────────────────────

def create_key(vendor, user, name, can_write=True):
    """Returns (ApiKey, the full key). The full key is never stored or shown again."""
    name = ' '.join(str(name or '').split())[:60]
    if not name:
        raise SellerApiError('Name the key after what will use it, e.g. "Warehouse sheet".')
    if ApiKey.objects.filter(vendor=vendor, revoked_at__isnull=True).count() >= MAX_KEYS:
        raise SellerApiError(f'Up to {MAX_KEYS} keys. Revoke one you no longer use.')
    prefix = secrets.token_hex(4)
    while ApiKey.objects.filter(prefix=prefix).exists():         # pragma: no cover - 1 in 4 billion
        prefix = secrets.token_hex(4)
    raw = f'nm_{prefix}_{secrets.token_urlsafe(32)}'
    key = ApiKey.objects.create(vendor=vendor, created_by=user, name=name, prefix=prefix, key_hash=_hash(raw),
                                can_write=bool(can_write))
    return key, raw


def find_key(raw):
    parts = str(raw or '').split('_', 2)
    if len(parts) != 3 or parts[0] != 'nm':
        return None
    key = (ApiKey.objects.select_related('vendor', 'vendor__user')
           .filter(prefix=parts[1], revoked_at__isnull=True).first())
    if key is None or not hmac.compare_digest(key.key_hash, _hash(raw)):
        return None
    now = timezone.now()
    if key.last_used_at is None or key.last_used_at < now - TOUCH_EVERY:    # one write per few minutes, not per call
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
    return key


class ApiKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = get_authorization_header(request).decode('latin-1')
        if not header.startswith('Bearer nm_'):
            return None
        key = find_key(header[len('Bearer '):].strip())
        if key is None:
            raise exceptions.AuthenticationFailed('Invalid or revoked API key.')
        return key.vendor.user, key

    def authenticate_header(self, request):
        return 'Bearer'


class HasApiKey(BasePermission):
    message = 'Your plan does not include the seller API. Upgrade in Seller Centre → Billing.'

    def has_permission(self, request, view):
        from payments.entitlements import plan_for_vendor
        from tools import registry
        key = request.auth
        if not isinstance(key, ApiKey):
            return False
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not key.can_write:
            self.message = 'This key is read-only.'
            return False
        return registry.included(plan_for_vendor(key.vendor), 'api')


class ApiKeyThrottle(SimpleRateThrottle):
    scope = 'seller_api'

    def get_cache_key(self, request, view):
        key = request.auth
        return self.cache_format % {'scope': self.scope, 'ident': key.pk} if isinstance(key, ApiKey) else None


# ── Reads ────────────────────────────────────────────────────────────────────

def _since(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise SellerApiError('updated_since: use an ISO 8601 time, e.g. 2026-11-20T08:00:00Z.')
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def _page_number(value):
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def products(vendor, params):
    """One row per simple product and per version, like bulk edit."""
    from product.models import Product, Variants
    from tools.bulk_edit import rows_for

    qs = Product.objects.filter(vendor=vendor).exclude(status='deleted')
    since = _since(params.get('updated_since'))
    if since:
        qs = qs.filter(updated__gte=since)
    sku = (params.get('seller_sku') or '').strip()
    if sku:
        qs = qs.filter(Q(seller_sku=sku) | Q(variants__seller_sku=sku)).distinct()
    if params.get('status'):
        qs = qs.filter(status=params['status'])
    number = _page_number(params.get('page'))
    total = qs.count()
    start = (number - 1) * PRODUCTS_PAGE
    page = list(qs.order_by('pk').prefetch_related(Prefetch('variants', queryset=Variants.objects.order_by('pk')))
                [start:start + PRODUCTS_PAGE])
    rows = [{k: v for k, v in r.items() if k != 'image'} for r in rows_for(page)]
    return {'count': total, 'page': number, 'has_more': start + PRODUCTS_PAGE < total, 'results': rows}


def orders(vendor, params):
    """The store's paid/placed orders with only its own lines, newest change first."""
    from order.models import Order, OrderProduct
    from tools.labels import _ship_to, number

    qs = Order.objects.filter(vendors=vendor, is_ordered=True)
    since = _since(params.get('updated_since'))
    if since:
        qs = qs.filter(date_updated__gte=since)
    if params.get('status'):
        qs = qs.filter(status=params['status'])
    page_no = _page_number(params.get('page'))
    total = qs.count()
    start = (page_no - 1) * ORDERS_PAGE
    page = list(qs.select_related('address', 'address__pickup_point', 'user')
                .order_by('-date_updated', '-pk')[start:start + ORDERS_PAGE])
    lines = {}
    for line in (OrderProduct.objects.filter(order__in=page, product__vendor=vendor)
                 .select_related('product', 'variant').order_by('pk')):
        lines.setdefault(line.order_id, []).append(line)
    return {'count': total, 'page': page_no, 'has_more': start + ORDERS_PAGE < total,
            'results': [order_json(o, lines.get(o.pk, []), _ship_to(o), number(o)) for o in page]}


def order_json(order, lines, ship_to, order_number):
    return {
        'id': order.pk, 'order_number': order_number, 'status': order.status,
        'payment_method': order.payment_method,
        'created_at': order.date_created.isoformat(), 'updated_at': order.date_updated.isoformat(),
        'ship_to': ship_to,
        'lines': [{
            'id': l.pk, 'product_id': l.product_id, 'variant_id': l.variant_id,
            'seller_sku': (l.variant.seller_sku if l.variant_id and l.variant else
                           l.product.seller_sku if l.product else ''),
            'title': l.product.title if l.product else '', 'quantity': l.quantity,
            'price': str(l.price), 'amount': str(l.amount), 'status': l.status,
        } for l in lines],
    }


# ── Writes ───────────────────────────────────────────────────────────────────

def update_stock(vendor, items):
    """
    items: [{seller_sku | product_id [+ variant_id], stock?, price?, compare_at_price?}]
    Rows it can't match are reported by index; the rest are saved together.
    """
    from product.models import Product, Variants
    from tools.bulk_edit import BulkEditError, apply_changes

    if not isinstance(items, list) or not items:
        raise SellerApiError('Send {"items": [...]} with at least one item.')
    if len(items) > MAX_STOCK_ITEMS:
        raise SellerApiError(f'Up to {MAX_STOCK_ITEMS} items per call.')

    skus = {str(i.get('seller_sku')).strip() for i in items if isinstance(i, dict) and i.get('seller_sku')}
    by_sku = {}
    if skus:
        for pid, sku, has_versions in (Product.objects.filter(vendor=vendor, seller_sku__in=skus)
                                       .values_list('pk', 'seller_sku', 'variant')):
            by_sku.setdefault(sku, []).append((pid, None) if has_versions == 'None' else ('versions', pid))
        for vid, pid, sku in (Variants.objects.filter(product__vendor=vendor, seller_sku__in=skus)
                              .values_list('pk', 'product_id', 'seller_sku')):
            by_sku.setdefault(sku, []).append((pid, vid))

    changes, errors, index_map = [], [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({'index': index, 'message': 'Each item must be an object.'})
            continue
        fields = {k: item[k] for k in ('stock', 'price', 'compare_at_price') if k in item}
        if not fields:
            errors.append({'index': index, 'message': 'Nothing to change: send stock, price or compare_at_price.'})
            continue
        if item.get('seller_sku'):
            found = [m for m in by_sku.get(str(item['seller_sku']).strip(), []) if m[0] != 'versions']
            if not found:
                versions = by_sku.get(str(item['seller_sku']).strip())
                errors.append({'index': index, 'message': 'This SKU is a product with versions: use the SKU of a version.'
                               if versions else 'No product or version with this seller_sku.'})
                continue
            if len(found) > 1:
                errors.append({'index': index, 'message': 'More than one item uses this seller_sku. Use product_id/variant_id.'})
                continue
            pid, vid = found[0]
        else:
            try:
                pid = int(item.get('product_id'))
                vid = int(item['variant_id']) if item.get('variant_id') else None
            except (TypeError, ValueError):
                errors.append({'index': index, 'message': 'Send seller_sku, or product_id (and variant_id for a version).'})
                continue
        changes.append({'product_id': pid, 'variant_id': vid, **fields})
        index_map.append(index)

    if not changes:
        return {'updated': 0, 'errors': errors}
    try:
        result = apply_changes(vendor, changes, source=PriceChange.API)
    except BulkEditError as exc:
        raise SellerApiError(str(exc))
    for e in result['errors']:
        errors.append({'index': index_map[e['index']], 'message': e['message']})
    errors.sort(key=lambda e: e['index'])
    return {'updated': result['updated'], 'errors': errors}

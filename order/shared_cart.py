"""
Share a cart with friends and family.

A shopper (signed in or a guest) turns their cart into a link:
    POST /api/v1/order/cart/share/             → {token, path, expires_at}
Anyone who opens it, signed in or not, sees the items at today's price and
stock and picks what to add to their own cart:
    GET  /api/v1/order/cart/shared/<token>/
    POST /api/v1/order/cart/shared/<token>/add/   {items: [{product_id, variant_id, quantity}]} (optional)

The link holds a snapshot of the lines (product, version, quantity), never
prices or the sharer's details beyond the first name they typed. Items that
were removed from sale since are shown as unavailable; quantities are cut to
what is in stock when they are added.
"""
import hashlib
import json
import re
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db.models import F
from django.utils import timezone

from core.service import get_exchange_rates
from order.cart_utils import handle_authenticated_cart, handle_guest_cart
from order.models import CartItem, SharedCart
from order.pricing import line_pricing
from product.models import Product, Variants

MAX_ITEMS = 50
MAX_QUANTITY = 99


class ShareError(ValueError):
    pass


def _clean_text(value, limit):
    """Plain, single-spaced text (the page renders it as text, never HTML)."""
    value = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', str(value or ''))
    value = re.sub(r'[ \t]+', ' ', value).strip()
    return re.sub(r'\n{3,}', '\n\n', value)[:limit]


def _line(product_id, variant_id, quantity):
    return {'product_id': int(product_id), 'variant_id': int(variant_id) if variant_id else None,
            'quantity': max(1, min(int(quantity), MAX_QUANTITY))}


def current_lines(request):
    """The requester's cart as [{product_id, variant_id, quantity}]."""
    if request.user.is_authenticated:
        return [_line(p, v, q) for p, v, q in CartItem.objects
                .filter(cart__user=request.user, quantity__gt=0)
                .order_by('id').values_list('product_id', 'variant_id', 'quantity')]
    lines = []
    for key, quantity in (request.session.get('guest_cart') or {}).items():
        try:
            product_id, variant_id = key.split('_', 1)
            lines.append(_line(product_id, None if variant_id == 'none' else variant_id, quantity))
        except (ValueError, TypeError):
            continue
    return lines


def create_share(request, sender_name='', note='', only=None):
    """
    A link for the requester's cart (or the lines in `only`, a list of
    "productId_variantId|none" keys). Sharing the same cart again returns the
    same link, with its expiry pushed back.
    """
    lines = current_lines(request)
    if only:
        wanted = {str(k) for k in only}
        lines = [l for l in lines if f"{l['product_id']}_{l['variant_id'] or 'none'}" in wanted]
    if not lines:
        raise ShareError('Your cart is empty. Add something before sharing it.')
    lines = lines[:MAX_ITEMS]
    sender_name = _clean_text(sender_name, 60).replace('\n', ' ')
    note = _clean_text(note, 300)

    user = request.user if request.user.is_authenticated else None
    if user is None and not request.session.session_key:
        request.session.save()
    session_key = '' if user else (request.session.session_key or '')
    owner = f"u{user.pk}" if user else f"s{session_key}"
    fingerprint = hashlib.sha256(json.dumps([owner, lines, sender_name, note], sort_keys=True).encode()).hexdigest()

    expires = timezone.now() + timedelta(days=SharedCart.EXPIRY_DAYS)
    share = SharedCart.objects.filter(fingerprint=fingerprint, expires_at__gt=timezone.now()).first()
    if share:
        SharedCart.objects.filter(pk=share.pk).update(expires_at=expires)
        share.expires_at = expires
        return share
    return SharedCart.objects.create(
        token=secrets.token_urlsafe(9), user=user, session_key=session_key, sender_name=sender_name,
        note=note, items=lines, fingerprint=fingerprint, expires_at=expires,
    )


def get_share(token):
    """The live share for `token`, or None (unknown or expired)."""
    if not token or len(token) > 24:
        return None
    share = SharedCart.objects.filter(token=token).first()
    if share is None or share.is_expired:
        return None
    return share


def _load(lines):
    """{(product_id, variant_id): (product, variant)} for the lines, in two queries."""
    products = {p.pk: p for p in Product.objects.filter(pk__in={l['product_id'] for l in lines})
                .select_related('vendor')}
    variant_ids = {l['variant_id'] for l in lines if l['variant_id']}
    variants = {v.pk: v for v in Variants.objects.filter(pk__in=variant_ids)} if variant_ids else {}
    found = {}
    for l in lines:
        product = products.get(l['product_id'])
        variant = variants.get(l['variant_id']) if l['variant_id'] else None
        if product is None or (l['variant_id'] and (variant is None or variant.product_id != product.pk)):
            continue
        found[(l['product_id'], l['variant_id'])] = (product, variant)
    return found


def _buyable(product):
    vendor = product.vendor
    return (product.status == 'published' and vendor is not None
            and not getattr(vendor, 'shop_paused', False) and not getattr(vendor, 'is_suspended', False))


def _image(request, product, variant):
    field = (variant.image if variant and variant.image else None) or product.image
    if not field:
        return None
    try:
        return request.build_absolute_uri(field.url)
    except ValueError:
        return None


def describe(share, request):
    """What the shared-cart page shows, at today's prices in the shopper's currency."""
    currency = request.headers.get('X-Currency', 'GHS')
    rate = Decimal(str(get_exchange_rates().get(currency, 1)))
    found = _load(share.items)
    items, total = [], Decimal('0')
    for l in share.items:
        pair = found.get((l['product_id'], l['variant_id']))
        if pair is None:
            items.append({'product_id': l['product_id'], 'variant_id': l['variant_id'], 'quantity': l['quantity'],
                          'status': 'unavailable', 'title': None})
            continue
        product, variant = pair
        stock = max(product.get_stock_quantity(variant), 0)
        buyable = _buyable(product)
        status = 'unavailable' if not buyable else 'out_of_stock' if stock <= 0 else \
            'limited' if stock < l['quantity'] else 'ok'
        quantity = min(l['quantity'], stock) if buyable and stock > 0 else l['quantity']
        pricing = line_pricing(product, variant, max(quantity, 1))
        unit = Decimal(str(pricing['unit_price']))
        old = product.old_price if product.old_price and product.old_price > unit else None
        if status in ('ok', 'limited'):
            total += unit * quantity
        items.append({
            'product_id': product.pk,
            'variant_id': variant.pk if variant else None,
            'title': product.title,
            'slug': product.slug,
            'sku': product.sku,
            'option_label': variant.option_label if variant else '',
            'image': _image(request, product, variant),
            'unit_price': float(round(unit * rate, 2)),
            'old_price': float(round(old * rate, 2)) if old else None,
            'is_flash_sale': pricing['flash_sale'] is not None,
            'quantity': quantity,
            'shared_quantity': l['quantity'],
            'available_stock': stock if buyable else 0,
            'status': status,
        })
    return {
        'token': share.token,
        'sender_name': share.sender_name,
        'note': share.note,
        'created_at': share.created_at,
        'expires_at': share.expires_at,
        'currency': currency,
        'items': items,
        'total': float(round(total * rate, 2)),
        'is_owner': is_owner(share, request),
    }


def is_owner(share, request):
    if request.user.is_authenticated:
        return share.user_id == request.user.pk
    return bool(share.session_key) and share.session_key == request.session.session_key


def record_view(share, request):
    if not is_owner(share, request):
        SharedCart.objects.filter(pk=share.pk).update(views=F('views') + 1)


def add_to_cart(share, request, picks=None):
    """
    Put the shared items (or the `picks` the shopper chose, with their
    quantities) into the requester's cart. Quantities are cut to what is in
    stock, counting what their cart already holds.
    Returns {added: [...], skipped: [...], total_cart_quantity}.
    """
    shared = {(l['product_id'], l['variant_id']): l['quantity'] for l in share.items}
    if picks is None:
        wanted = dict(shared)
    else:
        wanted = {}
        for pick in picks if isinstance(picks, list) else []:
            try:
                key = (int(pick.get('product_id')), int(pick['variant_id']) if pick.get('variant_id') else None)
                quantity = int(pick.get('quantity') or shared.get(key, 1))
            except (TypeError, ValueError, AttributeError):
                continue
            if key in shared and quantity > 0:            # only what was shared
                wanted[key] = min(quantity, MAX_QUANTITY)
    if not wanted:
        raise ShareError('Choose at least one item to add.')

    found = _load([{'product_id': p, 'variant_id': v} for p, v in wanted])
    added, skipped = [], []
    for (product_id, variant_id), quantity in wanted.items():
        pair = found.get((product_id, variant_id))
        if pair is None or not _buyable(pair[0]):
            skipped.append({'product_id': product_id, 'variant_id': variant_id, 'reason': 'unavailable'})
            continue
        product, variant = pair
        stock = max(product.get_stock_quantity(variant), 0)
        key = f"{product.pk}_{variant.pk if variant else 'none'}"
        if request.user.is_authenticated:
            in_cart = CartItem.objects.filter(cart__user=request.user, product=product, variant=variant) \
                .values_list('quantity', flat=True).first() or 0
        else:
            in_cart = (request.session.get('guest_cart') or {}).get(key, 0)
        room = stock - in_cart
        if room <= 0:
            skipped.append({'product_id': product_id, 'variant_id': variant_id, 'title': product.title,
                            'reason': 'out_of_stock' if stock <= 0 else 'already_at_max'})
            continue
        quantity_added = min(quantity, room)
        if request.user.is_authenticated:
            handle_authenticated_cart(request.user, product, variant, quantity_added)
        else:
            handle_guest_cart(request.session, key, quantity_added)
        added.append({'product_id': product_id, 'variant_id': variant_id, 'title': product.title,
                      'quantity': quantity_added, 'limited': quantity_added < quantity})

    if added and not is_owner(share, request):
        SharedCart.objects.filter(pk=share.pk).update(adds=F('adds') + 1)
    if request.user.is_authenticated:
        total_quantity = sum(CartItem.objects.filter(cart__user=request.user).values_list('quantity', flat=True))
    else:
        total_quantity = sum((request.session.get('guest_cart') or {}).values())
    return {'added': added, 'skipped': skipped, 'total_cart_quantity': total_quantity}

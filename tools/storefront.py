"""
tools/storefront.py
A store's own link and look:

  short link   negromart.com/s/<handle> sends shoppers to the store page with
               utm tags, counting visits (bots and repeat visits within 30
               minutes ignored). The QR code of that link is drawn in the
               Seller Centre (qrcode.react) for posters, packaging and WhatsApp.
  branding     accent colour, tagline, announcement bar and up to 8 featured
               products on the public store page, while the plan includes it.
               Texts pass the chat moderation (no phone numbers or outside links).
"""
import re

from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import F

from .models import StoreBranding, StoreLink

HANDLE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{1,28}[a-z0-9])$')
RESERVED = {'admin', 'api', 'negromart', 'support', 'help', 'seller', 'sellers', 'store', 'stores', 'shop', 'cart',
            'checkout', 'login', 'account', 'deals', 'official', 'staff', 'security', 'billing'}
HEX = re.compile(r'^#[0-9a-fA-F]{6}$')
MAX_FEATURED = 8
CACHE_SECONDS = 600
VISIT_DEDUPE = 30 * 60


class StoreToolError(ValueError):
    """Message is safe to show to the seller."""


def _moderate(text):
    from chat import moderation
    found = moderation.check(text)
    if found:
        raise StoreToolError(moderation.explain(found))


def _branding_key(vendor_id, request):
    # Cards are priced in the shopper's currency; the version is bumped on every change.
    currency = request.headers.get('X-Currency', 'GHS') if request is not None else 'GHS'
    version = cache.get(f'tools:branding:v:{vendor_id}', 0)
    return f'tools:branding:{vendor_id}:{version}:{currency}'


def _forget(vendor, handle=None):
    forget_featured(vendor.pk)
    keys = [f'vendor_metadata_v2:{vendor.slug}']
    if handle:
        keys.append(f'tools:storelink:{handle}')
    cache.delete_many(keys)


def set_handle(vendor, handle):
    handle = str(handle or '').strip().lower()
    if not HANDLE.fullmatch(handle):
        raise StoreToolError('Use 3 to 30 letters, numbers or dashes (not at the start or end).')
    if handle in RESERVED or 'negromart' in handle:
        raise StoreToolError('That name is reserved. Try another.')
    old = StoreLink.objects.filter(vendor=vendor).first()
    if StoreLink.objects.filter(handle=handle).exclude(vendor=vendor).exists():
        raise StoreToolError('Another store already uses that link.')
    try:
        link, _ = StoreLink.objects.update_or_create(vendor=vendor, defaults={'handle': handle})
    except IntegrityError:
        raise StoreToolError('Another store already uses that link.')
    _forget(vendor, old.handle if old else None)
    return link


def resolve(handle, request=None):
    """The store slug for a short link, counting the visit. None if unknown or the plan lapsed."""
    from payments.entitlements import plan_for_vendor
    from tools import registry

    handle = str(handle or '').lower()[:30]
    key = f'tools:storelink:{handle}'
    found = cache.get(key)
    if found is None:
        link = StoreLink.objects.select_related('vendor').filter(handle=handle).first()
        ok = bool(link and registry.included(plan_for_vendor(link.vendor), 'store_link'))
        found = {'slug': link.vendor.slug, 'id': link.pk} if ok else {}
        cache.set(key, found, CACHE_SECONDS)
    if found and request is not None:
        _count_visit(found['id'], request)
    return found.get('slug')


def _count_visit(link_id, request):
    from product.utils import _get_visitor_id, detect_bot
    try:
        if detect_bot(request):
            return
        # The shop site's /s/<handle> route forwards the shopper's visitor id and address.
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
        visitor = _get_visitor_id(request) or forwarded or request.META.get('REMOTE_ADDR', '')
        if cache.add(f'tools:storelink:seen:{link_id}:{visitor}', 1, VISIT_DEDUPE):
            StoreLink.objects.filter(pk=link_id).update(visits=F('visits') + 1)
    except Exception:       # counting must never break the redirect
        pass


def save_branding(vendor, accent_color='', tagline='', announcement='', featured_product_ids=()):
    from product.models import Product

    accent_color = str(accent_color or '').strip()
    if accent_color and not HEX.fullmatch(accent_color):
        raise StoreToolError('Accent colour: use a colour like #1E88E5.')
    tagline = ' '.join(str(tagline or '').split())[:80]
    announcement = ' '.join(str(announcement or '').split())[:140]
    if tagline or announcement:
        _moderate(f'{tagline}\n{announcement}')
    ids = []
    for i in featured_product_ids or []:
        if str(i).isdigit() and int(i) not in ids:
            ids.append(int(i))
    ids = ids[:MAX_FEATURED]
    if ids and Product.objects.filter(vendor=vendor, pk__in=ids, status='published').count() != len(ids):
        raise StoreToolError('Feature only your own live products.')
    branding, _ = StoreBranding.objects.update_or_create(vendor=vendor, defaults={
        'accent_color': accent_color, 'tagline': tagline, 'announcement': announcement, 'featured_product_ids': ids})
    _forget(vendor)
    return branding


def public_branding(vendor, request=None):
    """What the public store page shows, or None (not set, or the plan doesn't include it)."""
    from payments.entitlements import plan_for_vendor
    from product.cards import listing_rows
    from product.models import Product
    from tools import registry

    if not registry.included(plan_for_vendor(vendor), 'store_link'):
        return None
    key = _branding_key(vendor.pk, request)
    found = cache.get(key)
    if found is not None:
        return found or None
    b = StoreBranding.objects.filter(vendor=vendor).first()
    if b is None:
        cache.set(key, {}, CACHE_SECONDS)
        return None
    order = {pid: i for i, pid in enumerate(b.featured_product_ids)}
    products = sorted(Product.published.filter(pk__in=b.featured_product_ids, vendor=vendor), key=lambda p: order[p.pk])
    found = {'accent_color': b.accent_color, 'tagline': b.tagline, 'announcement': b.announcement,
             'featured': listing_rows(products, request) if products else []}
    cache.set(key, found, CACHE_SECONDS)
    return found


def forget_featured(vendor_id):
    """Branding or a featured product changed: every cached copy (all currencies) goes stale."""
    key = f'tools:branding:v:{vendor_id}'
    if not cache.add(key, 1, None):
        cache.incr(key)

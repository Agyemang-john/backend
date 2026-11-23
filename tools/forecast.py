"""
tools/forecast.py
How long each product (and each version) will last at the current sales pace,
and what to reorder.

Pace = a blend of the last 7 days (recent trend) and the last 28 days (steady
demand): 0.6 × daily(7) + 0.4 × daily(28). One grouped query over order lines
per store, whatever the number of products.

Reorder quantity = what you'll sell while the restock arrives (lead days) plus
the days you want covered afterwards, minus what you have.
"""
import math
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Prefetch, Q, Sum
from django.utils import timezone

from order.models import OrderProduct
from product.models import Product, Variants
from product.variations import effective_axes, option_label

SHORT, LONG = 7, 28
RECENT_WEIGHT = 0.6
CACHE_SECONDS = 300


def sales(vendor, now=None):
    """{(product_id, variant_id): (sold in 7 days, sold in 28 days)} for ordered, not cancelled lines."""
    now = now or timezone.now()
    rows = (OrderProduct.objects
            .filter(product__vendor=vendor, order__is_ordered=True, date_created__gte=now - timedelta(days=LONG))
            .exclude(status='canceled')
            .values('product_id', 'variant_id')
            .annotate(sold_long=Sum('quantity'),
                      sold_short=Sum('quantity', filter=Q(date_created__gte=now - timedelta(days=SHORT)))))
    return {(r['product_id'], r['variant_id']): (r['sold_short'] or 0, r['sold_long'] or 0) for r in rows}


def daily_rate(sold_short, sold_long):
    return RECENT_WEIGHT * sold_short / SHORT + (1 - RECENT_WEIGHT) * sold_long / LONG


def classify(stock, rate, lead_days):
    if stock <= 0:
        return 'out' if rate > 0 else 'out_idle'
    if rate <= 0:
        return 'no_sales'
    days = stock / rate
    if days <= lead_days:
        return 'reorder_now'      # will run out before a restock could arrive
    if days <= lead_days * 2:
        return 'reorder_soon'
    return 'ok'


ORDER = {'out': 0, 'reorder_now': 1, 'reorder_soon': 2, 'ok': 3, 'no_sales': 4, 'out_idle': 5}


def forecast(vendor, lead_days=7, cover_days=30, now=None):
    """Every live product/version with stock, pace, days left, status and reorder quantity."""
    lead_days = min(max(int(lead_days), 1), 120)
    cover_days = min(max(int(cover_days), 1), 365)
    key = f'tools:forecast:{vendor.pk}:{lead_days}:{cover_days}'
    if now is None:
        found = cache.get(key)
        if found is not None:
            return found

    sold = sales(vendor, now)
    products = (Product.objects.filter(vendor=vendor).exclude(status__in=['rejected', 'disabled'])
                .only('pk', 'title', 'sku', 'slug', 'image', 'variant', 'variation_axes', 'total_quantity', 'status')
                .prefetch_related(Prefetch('variants', queryset=Variants.objects.order_by('pk')
                                           .only('pk', 'product_id', 'options', 'size', 'color', 'quantity', 'image', 'seller_sku'))))
    rows = []
    for p in products:
        versions = list(p.variants.all()) if p.variant != 'None' else []
        axes = effective_axes(p) if versions else []
        items = [(v.pk, option_label(v, axes), v.quantity, v.seller_sku) for v in versions] or \
                [(None, '', p.total_quantity or 0, '')]
        for vid, label, stock, sku in items:
            short, long_ = sold.get((p.pk, vid), (0, 0))
            rate = daily_rate(short, long_)
            status = classify(stock, rate, lead_days)
            need = math.ceil(rate * (lead_days + cover_days)) - stock if rate > 0 else 0
            rows.append({
                'product_id': p.pk, 'variant_id': vid, 'title': p.title, 'version': label, 'seller_sku': sku,
                'sku': p.sku, 'slug': p.slug, 'image': p.image.url if p.image else None,
                'stock': stock, 'sold_7d': short, 'sold_28d': long_, 'per_day': round(rate, 2),
                'days_left': round(stock / rate, 1) if rate > 0 and stock > 0 else (0 if stock <= 0 else None),
                'status': status, 'reorder_qty': max(need, 0),
            })
    rows.sort(key=lambda r: (ORDER[r['status']], r['days_left'] if r['days_left'] is not None else 1e9, -r['sold_28d']))
    counts = {s: 0 for s in ORDER}
    for r in rows:
        counts[r['status']] += 1
    result = {'lead_days': lead_days, 'cover_days': cover_days, 'counts': counts, 'results': rows}
    if now is None:
        cache.set(key, result, CACHE_SECONDS)
    return result


# ── Daily alert ──────────────────────────────────────────────────────────────

ALERT_COOLDOWN = 7 * 24 * 3600


def alert_vendor(vendor, lead_days=7):
    """One notification listing items that will run out before a restock could arrive (each at most weekly)."""
    from notification.utils import send_notification

    due = [r for r in forecast(vendor, lead_days=lead_days, now=timezone.now())['results']
           if r['status'] in ('reorder_now', 'out')]
    fresh = []
    for r in due:
        key = f'tools:stock_alert:{vendor.pk}:{r["product_id"]}:{r["variant_id"]}'
        if cache.add(key, 1, ALERT_COOLDOWN):       # add() = only if not alerted this week
            fresh.append(r)
    if not fresh or vendor.user_id is None:
        return 0
    first = fresh[0]
    name = f'{first["title"]}{" (" + first["version"] + ")" if first["version"] else ""}'
    message = (f'{name} will run out in about {first["days_left"]:g} days.' if first['status'] == 'reorder_now'
               else f'{name} is out of stock and still selling.')
    if len(fresh) > 1:
        message += f' {len(fresh) - 1} more item{"s" if len(fresh) > 2 else ""} need restocking.'
    send_notification(vendor.user, 'vendor_stock_running_out',
                      data={'message': message, 'count': len(fresh), 'url': '/tools/stock'})
    return len(fresh)

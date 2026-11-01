"""
Price-drop and back-in-stock alerts.

`sweep()` runs every 15 minutes. It walks the watched items in chunks,
works out each item's live price (flash sales included) and stock with a
handful of set-based queries per chunk, and compares them with what each
alert saw last time. That catches every way a price or stock level changes —
seller edits, spreadsheet uploads, F()-updates from checkout and returns,
flash sales starting — without hooking each of those code paths.

Cost is O(watched items) per run with indexed lookups; notifications and
emails go out in batches (bulk_create + one SMTP connection per chunk).
"""
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from .emails import (absolute_media, base_context, build_email, opted_out_user_ids,
                     send_batch, site_url)
from .models import EmailOptOut, ProductAlert

logger = logging.getLogger(__name__)

CHUNK = 500
MIN_DROP_PERCENT = Decimal(getattr(settings, 'ALERT_MIN_DROP_PERCENT', 5))
# A price can bounce around; one shopper hears about one product at most this often.
PRICE_ALERT_COOLDOWN = timedelta(hours=getattr(settings, 'ALERT_PRICE_COOLDOWN_HOURS', 48))


# ── Live state ────────────────────────────────────────────────────────────────

def live_state(keys):
    """
    {(product_id, variant_id): {'price': Decimal, 'in_stock': bool, 'product': Product, 'variant': Variants|None}}
    for every (product_id, variant_id|None) in `keys`. Items that no longer
    exist are left out; unpublished/paused items count as out of stock.
    """
    from product.models import FlashSale, Product, Variants

    product_ids = {p for p, _ in keys}
    variant_ids = {v for _, v in keys if v}
    products = {p.pk: p for p in Product.objects.filter(pk__in=product_ids)
                .select_related('vendor').only('pk', 'title', 'slug', 'sku', 'image', 'price', 'variant',
                                               'total_quantity', 'status', 'vendor__shop_paused',
                                               'vendor__is_suspended', 'vendor__name')}
    variants = {v.pk: v for v in Variants.objects.filter(pk__in=variant_ids)
                .select_related('size', 'color').only('pk', 'product_id', 'price', 'quantity', 'title',
                                                      'size__name', 'color__name')}
    # Product-level stock for products sold in sizes/colours = sum of their variants.
    variant_stock = dict(Variants.objects.filter(product_id__in=product_ids)
                         .values_list('product_id').annotate(total=Sum('quantity')))

    now = timezone.now()
    sales = (FlashSale.objects.filter(product_id__in=product_ids, is_active=True,
                                      start_time__lte=now, end_time__gte=now)
             .filter(Q(max_quantity__isnull=True) | Q(max_quantity__gt=F('sold_count')))
             .values_list('product_id', 'variant_id', 'sale_price'))
    sale_price = {}
    for pid, vid, price in sales:
        key = (pid, vid)
        sale_price[key] = min(price, sale_price.get(key, price))

    state = {}
    for pid, vid in keys:
        product = products.get(pid)
        if product is None:
            continue
        variant = variants.get(vid) if vid else None
        if vid and (variant is None or variant.product_id != pid):
            continue
        sellable = (product.status == 'published' and product.vendor_id is not None
                    and not product.vendor.shop_paused and not product.vendor.is_suspended)
        if variant is not None:
            base, qty = variant.price, variant.quantity
            flash = sale_price.get((pid, vid), sale_price.get((pid, None)))
        else:
            base = product.price
            qty = (variant_stock.get(pid, 0) if product.variant != 'None' else product.total_quantity) or 0
            flash = sale_price.get((pid, None))
        price = min(base, flash) if flash is not None else base
        state[(pid, vid)] = {'price': price, 'in_stock': sellable and qty > 0,
                             'product': product, 'variant': variant}
    return state


def current_for(product, variant=None):
    """Live price/stock of one item (used when an alert is created)."""
    return live_state({(product.pk, variant.pk if variant else None)}).get(
        (product.pk, variant.pk if variant else None))


# ── Deciding who to tell ──────────────────────────────────────────────────────

def _price_trigger(alert, price, now):
    """True when this price is news for this shopper."""
    if alert.last_sent_at and now - alert.last_sent_at < PRICE_ALERT_COOLDOWN:
        return False
    if alert.target_price is not None:
        # The shopper named a price: only that counts, once per new low.
        return price <= alert.target_price and (
            alert.reference_price is None or price < alert.reference_price)
    if alert.reference_price is None or alert.reference_price <= 0:
        return False
    drop = (alert.reference_price - price) / alert.reference_price * 100
    return drop >= MIN_DROP_PERCENT


def sweep(now=None):
    """Check every active alert once. Returns counts for logging/tests."""
    now = now or timezone.now()
    counts = {'checked': 0, 'price': 0, 'stock': 0, 'emailed': 0}

    # Walk distinct watched items in id order so each chunk is one cheap range scan.
    items = (ProductAlert.objects.filter(is_active=True)
             .values_list('product_id', 'variant_id').distinct().order_by('product_id', 'variant_id'))
    batch = []
    for key in items.iterator(chunk_size=CHUNK * 4):
        batch.append(key)
        if len(batch) >= CHUNK:
            _sweep_items(batch, now, counts)
            batch = []
    if batch:
        _sweep_items(batch, now, counts)
    logger.info("alert sweep: %s", counts)
    return counts


def _sweep_items(keys, now, counts):
    state = live_state(set(keys))
    product_ids = {p for p, _ in keys}
    alerts = (ProductAlert.objects.filter(is_active=True, product_id__in=product_ids)
              .select_related('user').only('pk', 'user_id', 'product_id', 'variant_id', 'kind', 'target_price',
                                           'reference_price', 'last_seen_price', 'last_seen_in_stock',
                                           'sent_count', 'last_sent_at', 'user__email', 'user__first_name',
                                           'user__is_active'))
    fire = []          # (alert, item_state)
    to_update = []
    for alert in alerts:
        item = state.get((alert.product_id, alert.variant_id))
        if item is None:
            continue
        counts['checked'] += 1
        price, in_stock = item['price'], item['in_stock']
        changed = False

        if alert.kind == ProductAlert.PRICE:
            if in_stock and _price_trigger(alert, price, now):
                fire.append((alert, item))
                alert.was_price = alert.reference_price
                alert.reference_price = price
                alert.sent_count += 1
                alert.last_sent_at = now
                changed = True
            elif alert.reference_price is None:
                alert.reference_price = price
                changed = True
            if alert.last_seen_price != price:
                alert.last_seen_price = price
                changed = True
        else:  # STOCK: fire on out -> in, then switch off
            if in_stock and alert.last_seen_in_stock is False:
                fire.append((alert, item))
                alert.is_active = False
                alert.sent_count += 1
                alert.last_sent_at = now
                changed = True
            if alert.last_seen_in_stock != in_stock:
                alert.last_seen_in_stock = in_stock
                changed = True
        if changed:
            to_update.append(alert)

    if to_update:
        ProductAlert.objects.bulk_update(
            to_update, ['reference_price', 'last_seen_price', 'last_seen_in_stock', 'is_active',
                        'sent_count', 'last_sent_at'], batch_size=CHUNK)
    if fire:
        _deliver(fire, counts)


# ── Delivery ──────────────────────────────────────────────────────────────────

def item_url(product):
    return f"{site_url()}/{product.sku}/{product.slug}"


def _item_context(alert, item):
    product, variant = item['product'], item['variant']
    try:
        image = product.image.url if product.image else ''
    except ValueError:
        image = ''
    option = ''
    if variant is not None:
        option = ' / '.join(n for n in (getattr(variant.size, 'name', ''), getattr(variant.color, 'name', '')) if n)
    return {
        'title': product.title,
        'option': option,
        'price': item['price'],
        'was': getattr(alert, 'was_price', None),
        'image_url': absolute_media(image),
        'url': f"{item_url(product)}?utm_source=alert&utm_medium=email",
        'seller': getattr(product.vendor, 'name', '') or '',
    }


def _deliver(fire, counts):
    from notification.models import Notification
    from notification.utils import VERB_META, push_unread_count

    notifications, by_user = [], defaultdict(list)
    for alert, item in fire:
        if not alert.user.is_active:
            continue
        verb = 'customer_price_drop' if alert.kind == ProductAlert.PRICE else 'customer_back_in_stock'
        counts['price' if alert.kind == ProductAlert.PRICE else 'stock'] += 1
        product = item['product']
        meta = VERB_META[verb]
        price = f"GHS {item['price']:,.2f}"
        message = (f"{product.title} is now {price}." if alert.kind == ProductAlert.PRICE
                   else f"{product.title} is back in stock.")
        notifications.append(Notification(
            recipient_id=alert.user_id, verb=verb,
            data={**meta, 'message': message, 'url': f"/{product.sku}/{product.slug}",
                  'product_id': product.pk, 'price': str(item['price'])},
        ))
        by_user[alert.user_id].append((alert, item))

    Notification.objects.bulk_create(notifications, batch_size=CHUNK)
    for user_id in by_user:
        push_unread_count(user_id)

    blocked = opted_out_user_ids(list(by_user), EmailOptOut.ALERTS)
    messages = []
    for user_id, entries in by_user.items():
        user = entries[0][0].user
        if user_id in blocked or not user.email:
            continue
        messages.append(_email_for(user, entries))
    counts['emailed'] += send_batch(messages)


def _email_for(user, entries):
    price_items = [_item_context(a, i) for a, i in entries if a.kind == ProductAlert.PRICE]
    stock_items = [_item_context(a, i) for a, i in entries if a.kind == ProductAlert.STOCK]
    first = (price_items or stock_items)[0]['title']
    if len(entries) > 1:
        subject = "Good news about items you're watching"
    elif price_items:
        subject = f"Price drop: {first} is now GHS {price_items[0]['price']:,.2f}"
    else:
        subject = f"Back in stock: {first}"
    ctx = base_context(user, EmailOptOut.ALERTS)
    ctx.update({'price_items': price_items, 'stock_items': stock_items,
                'alerts_url': f"{site_url()}/dashboard/alerts"})
    return build_email(user, subject, 'product_alert', ctx)


# ── Create / remove (used by views and the wishlist signal) ──────────────────

@transaction.atomic
def watch(user, product, kind, variant=None, target_price=None, source=ProductAlert.MANUAL):
    """Create or switch back on an alert, seeded with the live state so only later changes count."""
    now_state = current_for(product, variant) or {}
    alert, created = ProductAlert.objects.get_or_create(
        user=user, product=product, variant=variant, kind=kind,
        defaults={'source': source},
    )
    alert.is_active = True
    if target_price is not None or source == ProductAlert.MANUAL:
        alert.target_price = target_price
    if source == ProductAlert.MANUAL:
        alert.source = ProductAlert.MANUAL   # an explicit ask outlives a wishlist removal
    price = now_state.get('price')
    alert.last_seen_price = price
    alert.last_seen_in_stock = now_state.get('in_stock')
    if created or alert.reference_price is None or (price is not None and price > alert.reference_price):
        alert.reference_price = price
    alert.save()
    return alert

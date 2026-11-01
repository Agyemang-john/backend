"""
Abandoned-cart reminders for signed-in shoppers.

Every 15 minutes `sweep()` finds carts that went quiet:
  stage 1: idle FIRST_AFTER (1h) .. SECOND_AFTER   — in-app + email
  stage 2: idle SECOND_AFTER (24h) .. GIVE_UP_AFTER (7d), only after stage 1 — email only
A cart's "version" is a fingerprint of its lines, so editing the cart restarts
the cycle while an untouched cart never gets more than two messages. Shoppers
who ordered since the cart went quiet, have nothing buyable left in it, or got
any cart reminder in the last MIN_GAP are skipped.

Conversions: an order within ATTRIBUTION_DAYS of a reminder is linked to it
(`attribute_orders`), which is what the admin report counts.

Cost: one indexed range scan over carts (partial index on updated_at for
signed-in carts), then per-chunk set queries for items, recent orders and
earlier reminders. Emails share one SMTP connection per chunk.
"""
import hashlib
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .emails import absolute_media, base_context, build_email, opted_out_user_ids, send_batch, site_url
from .models import CartReminder, EmailOptOut

logger = logging.getLogger(__name__)

FIRST_AFTER = timedelta(minutes=getattr(settings, 'CART_REMINDER_FIRST_MINUTES', 60))
SECOND_AFTER = timedelta(hours=getattr(settings, 'CART_REMINDER_SECOND_HOURS', 24))
GIVE_UP_AFTER = timedelta(days=7)
MIN_GAP = timedelta(hours=20)
ATTRIBUTION_DAYS = 7
CHUNK = 300
MAX_ITEMS_IN_EMAIL = 4


def fingerprint(lines):
    raw = '|'.join(sorted(f"{l.product_id}:{l.variant_id or 0}:{l.quantity}" for l in lines))
    return hashlib.sha1(raw.encode()).hexdigest()


def _buyable(line):
    p = line.product
    if p is None or p.status != 'published' or p.vendor is None:
        return False
    if p.vendor.shop_paused or p.vendor.is_suspended:
        return False
    stock = line.variant.quantity if line.variant_id and line.variant else p.total_quantity
    return (stock or 0) > 0


def sweep(now=None):
    from order.models import Cart

    now = now or timezone.now()
    counts = {'carts': 0, 'stage1': 0, 'stage2': 0, 'emailed': 0, 'converted': attribute_orders(now)}
    carts = (Cart.objects.filter(user__isnull=False, updated_at__lte=now - FIRST_AFTER,
                                 updated_at__gt=now - GIVE_UP_AFTER)
             .order_by('updated_at').values_list('pk', 'user_id', 'updated_at'))
    chunk = []
    for row in carts.iterator(chunk_size=CHUNK * 2):
        chunk.append(row)
        if len(chunk) >= CHUNK:
            _process(chunk, now, counts)
            chunk = []
    if chunk:
        _process(chunk, now, counts)
    logger.info("cart reminder sweep: %s", counts)
    return counts


def _process(rows, now, counts):
    from order.models import CartItem, Order

    cart_ids = [r[0] for r in rows]
    user_ids = [r[1] for r in rows]
    counts['carts'] += len(rows)

    lines = defaultdict(list)
    for item in (CartItem.objects.filter(cart_id__in=cart_ids, product__isnull=False)
                 .select_related('product__vendor', 'variant__size', 'variant__color')):
        lines[item.cart_id].append(item)

    # Shoppers who placed an order after their cart went quiet are done.
    oldest = min(r[2] for r in rows)
    ordered_after = defaultdict(lambda: None)
    for uid, created in (Order.objects.filter(user_id__in=user_ids, is_ordered=True, date_created__gte=oldest)
                         .values_list('user_id', 'date_created')):
        if ordered_after[uid] is None or created > ordered_after[uid]:
            ordered_after[uid] = created

    recent = defaultdict(list)   # user -> [(fingerprint, stage, sent_at)]
    for uid, fp, stage, sent in (CartReminder.objects.filter(user_id__in=user_ids, sent_at__gte=now - GIVE_UP_AFTER)
                                 .values_list('user_id', 'fingerprint', 'stage', 'sent_at')):
        recent[uid].append((fp, stage, sent))

    due = []   # (user_id, stage, fp, buyable lines)
    for cart_id, user_id, updated in rows:
        cart_lines = lines.get(cart_id)
        if not cart_lines:
            continue
        last_order = ordered_after[user_id]
        if last_order is not None and last_order >= updated:
            continue
        buyable = [l for l in cart_lines if _buyable(l)]
        if not buyable:
            continue
        fp = fingerprint(cart_lines)
        history = recent[user_id]
        if any(now - sent < MIN_GAP for _, _, sent in history):
            continue
        stages_sent = {stage for f, stage, _ in history if f == fp}
        idle = now - updated
        if CartReminder.FIRST not in stages_sent and idle < SECOND_AFTER:
            due.append((user_id, CartReminder.FIRST, fp, buyable))
        elif CartReminder.FIRST in stages_sent and CartReminder.SECOND not in stages_sent and idle >= SECOND_AFTER:
            due.append((user_id, CartReminder.SECOND, fp, buyable))
        # A cart first seen after a day (sweep was down, etc.) gets just one reminder:
        elif not stages_sent and idle >= SECOND_AFTER:
            due.append((user_id, CartReminder.SECOND, fp, buyable))

    if due:
        _deliver(due, counts)


def _line_context(line):
    from order.pricing import line_pricing
    pricing = line_pricing(line.product, line.variant, line.quantity)
    p = line.product
    try:
        image = p.image.url if p.image else ''
    except ValueError:
        image = ''
    option = ''
    if line.variant is not None:
        option = ' / '.join(n for n in (getattr(line.variant.size, 'name', ''),
                                        getattr(line.variant.color, 'name', '')) if n)
    return {
        'title': p.title, 'option': option, 'quantity': line.quantity,
        'price': pricing['unit_price'], 'amount': pricing['amount'],
        'on_sale': pricing['flash_sale'] is not None,
        'image_url': absolute_media(image),
        'url': f"{site_url()}/{p.sku}/{p.slug}?utm_source=cart_reminder&utm_medium=email",
    }


def _deliver(due, counts):
    from django.contrib.auth import get_user_model

    from notification.utils import send_notification

    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=[d[0] for d in due], is_active=True)
             .only('pk', 'email', 'first_name', 'is_active')}
    blocked = opted_out_user_ids(list(users), EmailOptOut.CART)
    messages, sent_rows = [], []
    for user_id, stage, fp, buyable in due:
        user = users.get(user_id)
        if user is None:
            continue
        items = [_line_context(l) for l in buyable]
        total = sum((i['amount'] for i in items), Decimal('0'))
        try:
            with transaction.atomic():
                reminder = CartReminder.objects.create(
                    user=user, stage=stage, fingerprint=fp, item_count=len(items), cart_value=total,
                )
        except IntegrityError:   # another sweep got here first
            continue
        counts['stage1' if stage == CartReminder.FIRST else 'stage2'] += 1
        if stage == CartReminder.FIRST:
            first = items[0]['title']
            more = f" and {len(items) - 1} more" if len(items) > 1 else ''
            send_notification(user, 'customer_cart_reminder', data={
                'message': f"{first}{more} {'is' if not more else 'are'} still in your cart.",
                'url': '/cart',
            })
        if user.email and user_id not in blocked:
            messages.append(_email(user, stage, items, total))
            sent_rows.append(reminder.pk)
    sent = send_batch(messages)
    counts['emailed'] += sent
    if sent:
        CartReminder.objects.filter(pk__in=sent_rows).update(emailed=True)


def _email(user, stage, items, total):
    first = items[0]['title']
    if stage == CartReminder.FIRST:
        subject = (f"Your {first} is waiting in your cart" if len(items) == 1
                   else f"You left {len(items)} items in your cart")
    else:
        subject = "Still thinking it over? Your cart is saved"
    on_sale = [i for i in items if i['on_sale']]
    ctx = base_context(user, EmailOptOut.CART)
    ctx.update({
        'stage': stage, 'items': items[:MAX_ITEMS_IN_EMAIL], 'more': max(0, len(items) - MAX_ITEMS_IN_EMAIL),
        'total': total, 'on_sale': bool(on_sale),
        'cart_url': f"{site_url()}/cart?utm_source=cart_reminder&utm_medium=email&utm_campaign=stage{stage}",
    })
    return build_email(user, subject, 'cart_reminder', ctx)


def attribute_orders(now=None):
    """Link orders placed within ATTRIBUTION_DAYS of a reminder to the latest such reminder."""
    from order.models import Order

    now = now or timezone.now()
    window_start = now - timedelta(days=ATTRIBUTION_DAYS + 1)
    pending = (CartReminder.objects.filter(order__isnull=True, converted_at__isnull=True, sent_at__gte=window_start)
               .filter(Exists(Order.objects.filter(user_id=OuterRef('user_id'), is_ordered=True,
                                                   date_created__gte=OuterRef('sent_at'))))
               .order_by('user_id', '-sent_at'))
    linked, seen = 0, set()
    for r in pending:
        if r.user_id in seen:
            continue
        seen.add(r.user_id)
        order = (Order.objects.filter(user_id=r.user_id, is_ordered=True, date_created__gte=r.sent_at,
                                      date_created__lte=r.sent_at + timedelta(days=ATTRIBUTION_DAYS))
                 .order_by('date_created').first())
        if order is None:
            continue
        CartReminder.objects.filter(pk=r.pk).update(order=order, converted_at=order.date_created)
        # Earlier reminders for the same cart don't double count.
        CartReminder.objects.filter(user_id=r.user_id, order__isnull=True, sent_at__lt=r.sent_at,
                                    sent_at__gte=window_start).update(converted_at=order.date_created)
        linked += 1
    return linked


def touch_cart(cart_id):
    """Cart lines changed: the cart is active again."""
    from order.models import Cart
    Cart.objects.filter(pk=cart_id).update(updated_at=timezone.now())

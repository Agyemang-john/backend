"""
tools/promotions.py
Promotions autopilot: automations a store switches on once.

  win_back     shoppers who bought from the store but not for `days` days get a
               personal coupon (once per `cooldown_days`).
  thank_you    `after_days` after a shopper's first order from the store is
               delivered, a personal coupon for the next one (once ever).
  weekly_sale  every week on chosen days and hours, a % off chosen products,
               created as ordinary flash sales through the seller flash-sale
               serializer, so every flash-sale guardrail applies.

Personal coupons are store coupons (the store pays the discount, ledger
SELLER_DISCOUNT) restricted to one shopper (Coupon.only_for), single use.
Shoppers get an in-app notification and an email unless they opted out of
"Offers from stores" (engagement.EmailOptOut.STORE_OFFERS).

run_all() is the scheduled entry point; each store's rules are independent and a
failure in one is recorded on the rule, never stops the others.
"""
import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Count, Max, Min
from django.utils import timezone

from order.models import OrderProduct
from product.models import Coupon, Product

from .models import PromotionRule, PromotionSale, PromotionSend

logger = logging.getLogger(__name__)

MAX_SENDS_PER_RUN = 300            # per rule per run; the rest go the next day
MAX_SALE_PRODUCTS = 5

DEFAULTS = {
    PromotionRule.WIN_BACK: {'days': 60, 'percent': 10, 'valid_days': 14, 'min_spend': None, 'cooldown_days': 90},
    PromotionRule.THANK_YOU: {'after_days': 3, 'percent': 10, 'valid_days': 30, 'min_spend': None},
    PromotionRule.WEEKLY_SALE: {'weekdays': [4], 'start': '18:00', 'hours': 3, 'percent': 15, 'product_ids': []},
}


class PromotionError(ValueError):
    """Message is safe to show to the seller."""


def _int(value, low, high, name):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise PromotionError(f'{name}: enter a whole number.')
    if not low <= number <= high:
        raise PromotionError(f'{name}: between {low} and {high}.')
    return number


def clean_settings(vendor, kind, raw):
    """Validate a rule's settings. Raises PromotionError."""
    if kind not in DEFAULTS:
        raise PromotionError('Unknown automation.')
    s = {**DEFAULTS[kind], **(raw or {})}
    out = {'percent': _int(s.get('percent'), 5, 70, 'Discount %')}
    if kind == PromotionRule.WEEKLY_SALE:
        days = s.get('weekdays') or []
        if not isinstance(days, list) or not days or any(not isinstance(d, int) or not 0 <= d <= 6 for d in days):
            raise PromotionError('Choose at least one day of the week.')
        try:
            start = datetime.strptime(str(s.get('start')), '%H:%M').time()
        except ValueError:
            raise PromotionError('Start time: use HH:MM, e.g. 18:00.')
        ids = [int(i) for i in (s.get('product_ids') or []) if str(i).isdigit()]
        if not ids:
            raise PromotionError('Choose the products that go on sale.')
        if len(ids) > MAX_SALE_PRODUCTS:
            raise PromotionError(f'Up to {MAX_SALE_PRODUCTS} products per weekly sale.')
        owned = set(Product.objects.filter(vendor=vendor, pk__in=ids, status='published').values_list('pk', flat=True))
        if owned != set(ids):
            raise PromotionError('Choose your own live products.')
        out.update(weekdays=sorted(set(days)), start=start.strftime('%H:%M'),
                   hours=_int(s.get('hours'), 1, 24, 'Hours'), product_ids=ids)
        return out
    out['valid_days'] = _int(s.get('valid_days'), 3, 90, 'Valid for (days)')
    min_spend = s.get('min_spend')
    if min_spend not in (None, ''):
        try:
            min_spend = Decimal(str(min_spend)).quantize(Decimal('0.01'))
        except Exception:
            raise PromotionError('Minimum spend: enter an amount.')
        if min_spend < 0:
            raise PromotionError('Minimum spend: enter an amount.')
    out['min_spend'] = str(min_spend) if min_spend not in (None, '') else None
    if kind == PromotionRule.WIN_BACK:
        out['days'] = _int(s.get('days'), 14, 365, 'Days since last order')
        out['cooldown_days'] = _int(s.get('cooldown_days'), 30, 365, 'Send again after (days)')
    else:
        out['after_days'] = _int(s.get('after_days'), 1, 30, 'Days after delivery')
    return out


# ── Personal coupons ─────────────────────────────────────────────────────────

def _code(vendor):
    return f'FOR{vendor.pk}-{secrets.token_hex(3).upper()}'


def _coupon(rule, user, now):
    s = rule.settings
    for _ in range(5):          # a clash on the random code is astronomically rare; retry anyway
        code = _code(rule.vendor)
        if not Coupon.objects.filter(code=code).exists():
            break
    return Coupon.objects.create(
        code=code, vendor=rule.vendor, discount_percentage=float(s['percent']), valid_from=now,
        valid_to=now + timedelta(days=s['valid_days']), max_uses=1, per_user_limit=1, only_for=user,
        min_purchase_amount=Decimal(s['min_spend']) if s.get('min_spend') else None,
    )


def _buyers(vendor):
    """Lines of the store that count as purchases (ordered, not cancelled)."""
    return (OrderProduct.objects.filter(product__vendor=vendor, order__is_ordered=True, order__user__isnull=False)
            .exclude(status='canceled'))


def win_back_targets(rule, now):
    s = rule.settings
    lapsed = (_buyers(rule.vendor).values('order__user_id').annotate(last=Max('date_created'))
              .filter(last__lt=now - timedelta(days=s['days'])).values_list('order__user_id', flat=True))
    recent = PromotionSend.objects.filter(rule=rule, created_at__gte=now - timedelta(days=s['cooldown_days'])) \
        .values_list('user_id', flat=True)
    return list(set(lapsed) - set(recent))[:MAX_SENDS_PER_RUN]


def thank_you_targets(rule, now):
    s = rule.settings
    window_end = now - timedelta(days=s['after_days'])
    window_start = window_end - timedelta(days=7)     # a missed day still gets sent, an old order doesn't
    firsts = (_buyers(rule.vendor).values('order__user_id')
              .annotate(orders=Count('order', distinct=True), first_delivered=Min('delivered_date'))
              .filter(orders=1, first_delivered__gte=window_start, first_delivered__lt=window_end)
              .values_list('order__user_id', flat=True))
    ever = PromotionSend.objects.filter(rule=rule).values_list('user_id', flat=True)
    return list(set(firsts) - set(ever))[:MAX_SENDS_PER_RUN]


def _deliver(rule, user_ids, now):
    from django.contrib.auth import get_user_model

    from engagement.emails import base_context, build_email, opted_out_user_ids, send_batch, site_url
    from engagement.models import EmailOptOut
    from notification.utils import send_notification

    users = list(get_user_model().objects.filter(pk__in=user_ids, is_active=True).only('pk', 'email', 'first_name'))
    blocked = opted_out_user_ids([u.pk for u in users], EmailOptOut.STORE_OFFERS)
    store = rule.vendor
    store_url = f'{site_url()}/seller/{store.slug}' if getattr(store, 'slug', '') else site_url()
    messages, sends = [], []
    for user in users:
        with transaction.atomic():
            coupon = _coupon(rule, user, now)
            send = PromotionSend.objects.create(rule=rule, user=user, coupon=coupon)
        percent = rule.settings['percent']
        headline = (f'{percent}% off your next order from {store.name}' if rule.kind == PromotionRule.THANK_YOU
                    else f'We miss you: {percent}% off at {store.name}')
        send_notification(user, 'customer_store_offer', data={
            'message': f'{headline}. Use code {coupon.code} by {coupon.valid_to:%d %b}.',
            'url': f'/seller/{store.slug}' if getattr(store, 'slug', '') else '/', 'code': coupon.code})
        if user.email and user.pk not in blocked:
            ctx = {**base_context(user, EmailOptOut.STORE_OFFERS), 'store_name': store.name, 'store_url': store_url,
                   'headline': headline, 'code': coupon.code, 'percent': percent, 'valid_to': coupon.valid_to,
                   'min_spend': rule.settings.get('min_spend'),
                   'intro': ('Thank you for your order. Here is something for the next one.'
                             if rule.kind == PromotionRule.THANK_YOU else
                             "It's been a while. Here's a little something to welcome you back.")}
            messages.append(build_email(user, headline, 'store_offer', ctx))
            sends.append(send.pk)
    sent = send_batch(messages)
    if sent and sends:
        PromotionSend.objects.filter(pk__in=sends[:sent]).update(emailed=True)
    return len(users)


# ── Weekly sale ──────────────────────────────────────────────────────────────

def next_occurrences(settings_, now):
    """Start datetimes (local time) of the sale in the next 7 days, not already over."""
    tz = timezone.get_current_timezone()
    start = datetime.strptime(settings_['start'], '%H:%M').time()
    today = timezone.localtime(now, tz).date()
    out = []
    for offset in range(8):
        day = today + timedelta(days=offset)
        if day.weekday() in settings_['weekdays']:
            begins = timezone.make_aware(datetime.combine(day, start), tz)
            if begins + timedelta(hours=settings_['hours']) > now:
                out.append(begins)
    return out[:1]          # only the next one: keeps under the store's open-sales limit


def schedule_weekly_sale(rule, now):
    from vendor.flash_sale_views import SellerFlashSaleSerializer

    s = rule.settings
    created, problems = 0, []
    for begins in next_occurrences(s, now):
        for product in Product.objects.filter(pk__in=s['product_ids'], vendor=rule.vendor):
            if PromotionSale.objects.filter(rule=rule, starts_at=begins, flash_sale__product=product).exists():
                continue
            price = product.price
            sale_price = (price * (Decimal(100) - s['percent']) / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            ser = SellerFlashSaleSerializer(data={
                'product': product.pk, 'sale_price': str(sale_price), 'start_time': max(begins, now).isoformat(),
                'end_time': (begins + timedelta(hours=s['hours'])).isoformat(), 'label': 'limited', 'is_active': True,
            }, context={'vendor': rule.vendor})
            if ser.is_valid():
                sale = ser.save()
                PromotionSale.objects.create(rule=rule, flash_sale=sale, starts_at=begins)
                created += 1
            else:
                problems.append(f'{product.title[:40]}: {_first_error(ser.errors)}')
    return created, problems


def _first_error(errors):
    if isinstance(errors, dict):
        value = next(iter(errors.values()), '')
    else:
        value = errors
    while isinstance(value, (list, tuple)) and value:
        value = value[0]
    return str(value)[:120]


def cancel_upcoming(rule):
    """Switching a weekly sale off cancels the sales it scheduled that haven't started."""
    from product.models import FlashSale
    ids = PromotionSale.objects.filter(rule=rule, flash_sale__start_time__gt=timezone.now()).values_list('flash_sale_id', flat=True)
    FlashSale.objects.filter(pk__in=list(ids), sold_count=0).delete()


# ── Running ──────────────────────────────────────────────────────────────────

def run_rule(rule, now=None):
    now = now or timezone.now()
    if rule.kind == PromotionRule.WEEKLY_SALE:
        created, problems = schedule_weekly_sale(rule, now)
        result = f'{created} sale{"s" if created != 1 else ""} scheduled.' if created else 'Next sale already scheduled.'
        if problems:
            result = f'{result} Skipped: {"; ".join(problems)}'
    else:
        targets = win_back_targets(rule, now) if rule.kind == PromotionRule.WIN_BACK else thank_you_targets(rule, now)
        sent = _deliver(rule, targets, now) if targets else 0
        result = f'{sent} coupon{"s" if sent != 1 else ""} sent.' if sent else 'Nobody due today.'
    PromotionRule.objects.filter(pk=rule.pk).update(last_run_at=now, last_result=result[:200])
    return result


def run_all(now=None):
    """Daily: every active rule of stores whose plan includes the autopilot."""
    from payments.entitlements import plan_for_vendor
    from tools import registry

    done = 0
    allowed = {}
    for rule in PromotionRule.objects.filter(is_active=True).select_related('vendor').iterator(chunk_size=200):
        if rule.vendor_id not in allowed:
            allowed[rule.vendor_id] = registry.included(plan_for_vendor(rule.vendor), 'promotions')
        if not allowed[rule.vendor_id]:
            continue
        try:
            run_rule(rule, now)
            done += 1
        except Exception:
            logger.exception('Promotion rule %s failed', rule.pk)
            PromotionRule.objects.filter(pk=rule.pk).update(last_run_at=now or timezone.now(),
                                                             last_result='Something went wrong; we will try again tomorrow.')
    return done


def stats(rule):
    """Sent, used and the money the coupons brought in."""
    from product.models import CouponRedemption
    if rule.kind == PromotionRule.WEEKLY_SALE:
        sales = PromotionSale.objects.filter(rule=rule).select_related('flash_sale')
        return {'sales': sales.count(), 'units_sold': sum(s.flash_sale.sold_count for s in sales)}
    coupons = PromotionSend.objects.filter(rule=rule).values_list('coupon_id', flat=True)
    redemptions = CouponRedemption.objects.filter(coupon_id__in=coupons).select_related('order')
    return {
        'sent': PromotionSend.objects.filter(rule=rule).count(),
        'used': redemptions.count(),
        'order_value': str(sum((r.order.total for r in redemptions if r.order_id), Decimal('0'))),
        'discount_given': str(sum((r.amount or 0 for r in redemptions), Decimal('0'))),
    }

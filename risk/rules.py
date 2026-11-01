"""
Fraud rules. Each returns points (0 = didn't fire) and a label for staff.
Scoring is additive and capped at 100:
    < REVIEW_AT        low     – nothing happens
    REVIEW_AT..HOLD_AT review  – listed for staff, order proceeds
    >= HOLD_AT         high    – held: no shipment until staff approve

Every rule is one indexed query (or none), so scoring stays O(1) per order.
Thresholds live in settings so they can be tuned without a deploy of code.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

REVIEW_AT = getattr(settings, 'RISK_REVIEW_AT', 40)
HOLD_AT = getattr(settings, 'RISK_HOLD_AT', 70)
HIGH_VALUE = Decimal(str(getattr(settings, 'RISK_HIGH_VALUE_GHS', '2000')))


def _new_account_high_value(order, user):
    age = order.date_created - user.date_joined
    if age < timedelta(hours=24) and Decimal(order.total) >= HIGH_VALUE:
        return 35, f"Account under a day old placing a GHS {order.total} order"
    if age < timedelta(hours=24) and Decimal(order.total) >= HIGH_VALUE / 4:
        return 15, "Account under a day old"
    return 0, ''


def _velocity(order, user):
    from order.models import Order
    recent = Order.objects.filter(user=user, is_ordered=True, date_created__gte=order.date_created - timedelta(hours=1),
                                  date_created__lte=order.date_created).count()
    if recent >= 5:
        return 30, f"{recent} orders in the last hour"
    if recent >= 3:
        return 15, f"{recent} orders in the last hour"
    return 0, ''


def _ip_velocity(order, user):
    from order.models import Order
    if not order.ip:
        return 0, ''
    accounts = (Order.objects.filter(ip=order.ip, is_ordered=True, date_created__gte=order.date_created - timedelta(hours=24))
                .values('user_id').distinct().count())
    if accounts >= 4:
        return 25, f"{accounts} accounts ordered from this network today"
    return 0, ''


def _unverified(order, user):
    if not user.email_verified_at and not user.phone_verified_at:
        return 15, "Neither email nor phone verified"
    return 0, ''


def _cod_refusals(order, user):
    if order.payment_method != 'cash_on_delivery':
        return 0, ''
    n = refused_cod_count(user)
    if n >= 2:
        return 40, f"{n} earlier pay-on-delivery orders refused or cancelled"
    if n == 1:
        return 15, "An earlier pay-on-delivery order was refused or cancelled"
    return 0, ''


def _shared_address(order, user):
    from address.models import Address
    a = order.address
    if a is None or not (a.address or '').strip():
        return 0, ''
    others = (Address.objects.filter(address__iexact=a.address.strip(), town__iexact=(a.town or '').strip())
              .exclude(user=user).values('user_id').distinct().count())
    if others >= 3:
        return 20, f"Delivery address used by {others} other accounts"
    return 0, ''


def _bulk_quantity(order, user):
    big = order.order_products.filter(quantity__gte=10).count()
    if big:
        return 10, "Unusually large quantity of one item"
    return 0, ''


RULES = [
    ('new_account', _new_account_high_value),
    ('velocity', _velocity),
    ('ip_velocity', _ip_velocity),
    ('unverified', _unverified),
    ('cod_refusals', _cod_refusals),
    ('shared_address', _shared_address),
    ('bulk_quantity', _bulk_quantity),
]


def refused_cod_count(user, days=180):
    """Pay-on-delivery orders that were cancelled (refused at the door or abandoned) recently."""
    from order.models import Order
    since = timezone.now() - timedelta(days=days)
    return Order.objects.filter(user=user, payment_method='cash_on_delivery', status='canceled',
                                date_created__gte=since).count()


def score(order):
    """(score, level, reasons) for an order."""
    from .models import OrderRisk
    user = order.user
    reasons = []
    if user is not None:
        for code, rule in RULES:
            points, label = rule(order, user)
            if points:
                reasons.append({'code': code, 'label': label, 'points': points})
    total = min(100, sum(r['points'] for r in reasons))
    level = OrderRisk.HIGH if total >= HOLD_AT else OrderRisk.REVIEW if total >= REVIEW_AT else OrderRisk.LOW
    return total, level, reasons

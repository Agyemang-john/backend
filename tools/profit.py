"""
tools/profit.py
A store's real profit: what it earned on the ledger (sales, minus its own
coupons, commission, refunds and ad spend, plus delivery earnings) minus what
the goods cost it (quantity sold × the seller's unit cost).

Everything is aggregated in the database (two or three grouped queries per
report, whatever the order volume), from payments.LedgerEntry, where every
amount is signed by its effect on the seller's balance. Sales count when they
are posted to the ledger, i.e. when the order is delivered.

Cost of goods uses the cost price the seller has set now (version cost, else
product cost); lines without one are counted so the report can say so.
"""
import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from payments.ledger_models import LedgerEntry

ZERO = Decimal('0.00')
MAX_DAYS = 731
CACHE_SECONDS = 600
MONEY = DecimalField(max_digits=14, decimal_places=2)


def _bounds(start, end):
    today = timezone.localdate()
    end = min(end or today, today)
    start = start or end - timedelta(days=89)
    if start > end:
        start, end = end, start
    start = max(start, end - timedelta(days=MAX_DAYS))
    tz = timezone.get_current_timezone()
    return start, end, timezone.make_aware(datetime.combine(start, time.min), tz), \
        timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz)


def _sums(qs):
    t = LedgerEntry
    return qs.aggregate(
        sales=Sum('amount', filter=Q(entry_type=t.SALE)),
        coupons=Sum('amount', filter=Q(entry_type=t.SELLER_DISCOUNT)),
        commission=Sum('amount', filter=Q(entry_type__in=[t.COMMISSION, t.COMMISSION_REFUND])),
        refunds=Sum('amount', filter=Q(entry_type=t.REFUND)),
        delivery=Sum('amount', filter=Q(entry_type=t.DELIVERY_EARNING)),
        ads=Sum('amount', filter=Q(entry_type=t.AD_SPEND)),
        adjustments=Sum('amount', filter=Q(entry_type=t.ADJUSTMENT)),
    )


UNIT_COST = Coalesce('order_product__variant__unit_cost', 'order_product__product__unit_cost')
LINE_COST = ExpressionWrapper(F('order_product__quantity') * UNIT_COST, output_field=MONEY)


def _cogs(qs):
    """Cost of goods for the SALE rows of qs: (total, units, units without a cost price)."""
    sold = qs.filter(entry_type=LedgerEntry.SALE)
    row = sold.aggregate(cost=Sum(LINE_COST), units=Sum('order_product__quantity'),
                         units_no_cost=Sum('order_product__quantity', filter=Q(
                             order_product__variant__unit_cost__isnull=True,
                             order_product__product__unit_cost__isnull=True)))
    return row['cost'] or ZERO, row['units'] or 0, row['units_no_cost'] or 0


def _summary(s, cost):
    s = {k: (v or ZERO) for k, v in s.items()}
    earned = sum(s.values(), ZERO)
    profit = earned - cost
    return {**{k: str(v) for k, v in s.items()}, 'earned': str(earned), 'cost_of_goods': str(cost),
            'profit': str(profit),
            'margin': round(float(profit / s['sales'] * 100), 1) if s['sales'] else None}


def report(vendor, start=None, end=None):
    start, end, since, until = _bounds(start, end)
    key = f'tools:profit:{vendor.pk}:{start}:{end}'
    found = cache.get(key)
    if found is not None:
        return found

    entries = LedgerEntry.objects.filter(vendor=vendor, created_at__gte=since, created_at__lt=until) \
        .exclude(entry_type=LedgerEntry.PAYOUT)
    cost, units, units_no_cost = _cogs(entries)
    totals = _summary(_sums(entries), cost)

    # By month
    months = {}
    t = LedgerEntry
    for row in (entries.annotate(month=TruncMonth('created_at')).values('month').annotate(
            sales=Sum('amount', filter=Q(entry_type=t.SALE)),
            earned=Sum('amount'),
            cost=Sum(LINE_COST, filter=Q(entry_type=t.SALE))).order_by('month')):
        cost_m = row['cost'] or ZERO
        earned = row['earned'] or ZERO
        months[row['month'].date().isoformat()[:7]] = {
            'month': row['month'].date().isoformat()[:7], 'sales': str(row['sales'] or ZERO),
            'earned': str(earned), 'cost_of_goods': str(cost_m), 'profit': str(earned - cost_m)}

    # By product (ads are store-wide, so they are not split across products)
    products = []
    for row in (entries.filter(order_product__isnull=False)
                .values('order_product__product_id', 'order_product__product__title')
                .annotate(
                    sales=Sum('amount', filter=Q(entry_type=t.SALE)),
                    coupons=Sum('amount', filter=Q(entry_type=t.SELLER_DISCOUNT)),
                    commission=Sum('amount', filter=Q(entry_type__in=[t.COMMISSION, t.COMMISSION_REFUND])),
                    refunds=Sum('amount', filter=Q(entry_type=t.REFUND)),
                    delivery=Sum('amount', filter=Q(entry_type=t.DELIVERY_EARNING)),
                    earned=Sum('amount'),
                    units=Sum('order_product__quantity', filter=Q(entry_type=t.SALE)),
                    cost=Sum(LINE_COST, filter=Q(entry_type=t.SALE)),
                    no_cost=Count('pk', filter=Q(entry_type=t.SALE, order_product__variant__unit_cost__isnull=True,
                                                 order_product__product__unit_cost__isnull=True)))):
        earned = row['earned'] or ZERO
        cost_p = row['cost'] or ZERO
        sales = row['sales'] or ZERO
        profit = earned - cost_p
        products.append({
            'product_id': row['order_product__product_id'], 'title': row['order_product__product__title'] or 'Deleted product',
            'units': row['units'] or 0, 'sales': str(sales), 'coupons': str(row['coupons'] or ZERO),
            'commission': str(row['commission'] or ZERO), 'refunds': str(row['refunds'] or ZERO),
            'delivery': str(row['delivery'] or ZERO), 'cost_of_goods': str(cost_p), 'profit': str(profit),
            'margin': round(float(profit / sales * 100), 1) if sales else None,
            'missing_cost': bool(row['no_cost']),
        })
    products.sort(key=lambda p: Decimal(p['profit']), reverse=True)

    result = {
        'start': start.isoformat(), 'end': end.isoformat(), 'totals': totals,
        'units_sold': units, 'units_without_cost': units_no_cost,
        'months': list(months.values()), 'products': products,
    }
    cache.set(key, result, CACHE_SECONDS)
    return result


def report_csv(vendor, start=None, end=None):
    data = report(vendor, start, end)
    out = io.StringIO()
    fields = ['product_id', 'title', 'units', 'sales', 'coupons', 'commission', 'refunds', 'delivery',
              'cost_of_goods', 'profit', 'margin', 'missing_cost']
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(data['products'])
    t = data['totals']
    writer.writerow({'title': 'TOTAL (ads and adjustments included)', 'units': data['units_sold'], 'sales': t['sales'],
                     'coupons': t['coupons'], 'commission': t['commission'], 'refunds': t['refunds'],
                     'delivery': t['delivery'], 'cost_of_goods': t['cost_of_goods'], 'profit': t['profit'],
                     'margin': t['margin']})
    return '﻿' + out.getvalue()


def parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None

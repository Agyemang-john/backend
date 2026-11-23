"""
tools/pricing.py
Price insights and the auto-repricer.

Insights, per listing:
  - sold by other stores too (catalog/ offers): the cheapest other offer that
    can be bought now, how many there are, and whether you hold the buy box;
  - otherwise: where your price sits in its category (25th / 50th / 75th
    percentile of live prices), computed in Postgres and cached per category.

Repricer (run_repricer, every 15 minutes): for each active rule, target the
cheapest other offer (beat it by `step`, or match it), never outside the
seller's min/max. One query for all competing offers of all rules in a batch.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.db.models import Aggregate, FloatField
from django.utils import timezone

from product.models import Product, Variants

CATEGORY_CACHE_SECONDS = 3600
PAGE_SIZE = 30
REPRICER_BATCH = 500
AT_MINIMUM_NOTICE = 24 * 3600


class Percentile(Aggregate):
    """Postgres percentile_cont(fraction) WITHIN GROUP (ORDER BY expr)."""
    function = 'percentile_cont'
    template = '%(function)s(%(fraction)s) WITHIN GROUP (ORDER BY %(expressions)s)'
    output_field = FloatField()

    def __init__(self, expression, fraction, **extra):
        super().__init__(expression, fraction=fraction, **extra)


def _live(qs):
    """Offers a shopper could buy right now."""
    return qs.filter(status='published', vendor__is_approved=True, vendor__shop_paused=False,
                     vendor__is_suspended=False)


def _in_stock_ids(products):
    """Ids of products in stock (simple: total_quantity; with versions: any version)."""
    simple = {p.pk for p in products if p.variant == 'None' and (p.total_quantity or 0) > 0}
    with_versions = [p.pk for p in products if p.variant != 'None']
    if with_versions:
        simple |= set(Variants.objects.filter(product_id__in=with_versions, quantity__gt=0)
                      .values_list('product_id', flat=True).distinct())
    return simple


def competing_offers(item_ids):
    """{catalog_item_id: [(product_id, vendor_id, price)]} for live, in-stock offers."""
    offers = list(_live(Product.objects.filter(catalog_item_id__in=item_ids))
                  .only('pk', 'catalog_item_id', 'vendor_id', 'price', 'variant', 'total_quantity'))
    stocked = _in_stock_ids(offers)
    by_item = defaultdict(list)
    for p in offers:
        if p.pk in stocked:
            by_item[p.catalog_item_id].append((p.pk, p.vendor_id, p.price))
    return by_item


def category_range(sub_category_id):
    """25th/50th/75th percentile of live prices in a category (cached)."""
    key = f'tools:price_range:{sub_category_id}'
    found = cache.get(key)
    if found is not None:
        return found
    row = _live(Product.objects.filter(sub_category_id=sub_category_id)).aggregate(
        p25=Percentile('price', 0.25), p50=Percentile('price', 0.5), p75=Percentile('price', 0.75))
    found = {k: (round(Decimal(str(v)), 2) if v is not None else None) for k, v in row.items()}
    count = _live(Product.objects.filter(sub_category_id=sub_category_id)).count()
    found['count'] = count
    cache.set(key, found, CATEGORY_CACHE_SECONDS)
    return found


def insights(vendor, number=1, only='all'):
    from tools.models import RepricerRule

    qs = (Product.objects.filter(vendor=vendor, status='published')
          .only('pk', 'title', 'sku', 'slug', 'image', 'price', 'variant', 'catalog_item_id', 'sub_category_id',
                'is_buybox_winner', 'total_quantity'))
    if only == 'shared':
        qs = qs.filter(catalog_item__isnull=False)
    total = qs.count()
    start = (max(number, 1) - 1) * PAGE_SIZE
    products = list(qs.order_by('catalog_item_id', 'title')[start:start + PAGE_SIZE])
    offers = competing_offers({p.catalog_item_id for p in products if p.catalog_item_id})
    rules = {r.product_id: r for r in RepricerRule.objects.filter(product__in=products)}

    rows = []
    for p in products:
        row = {'product_id': p.pk, 'title': p.title, 'sku': p.sku, 'slug': p.slug,
               'image': p.image.url if p.image else None, 'price': str(p.price), 'has_versions': p.variant != 'None'}
        if p.catalog_item_id:
            others = [price for pid, vid, price in offers.get(p.catalog_item_id, []) if vid != vendor.pk]
            lowest = min(others) if others else None
            row.update({
                'kind': 'shared', 'other_offers': len(others), 'lowest_other': str(lowest) if lowest is not None else None,
                'buy_box': bool(p.is_buybox_winner),
                'gap': str(p.price - lowest) if lowest is not None else None,
                'advice': ('You have the lowest price.' if lowest is None or p.price <= lowest else
                           f'₵{p.price - lowest:.2f} above the cheapest seller.'),
            })
        else:
            r = category_range(p.sub_category_id) if p.sub_category_id else {'count': 0}
            if r.get('p25') is None or r['count'] < 5:
                advice, band = 'Not enough similar listings to compare yet.', None
            elif p.price < r['p25']:
                advice, band = 'Cheaper than most in this category: room to raise the price?', 'low'
            elif p.price > r['p75']:
                advice, band = 'Pricier than most in this category: make sure photos and details justify it.', 'high'
            else:
                advice, band = 'In the usual range for this category.', 'typical'
            row.update({'kind': 'category', 'range': {k: (str(v) if v is not None else None) for k, v in r.items()
                                                      if k != 'count'},
                        'compared_with': r.get('count', 0), 'band': band, 'advice': advice})
        rule = rules.get(p.pk)
        row['repricer'] = rule_json(rule) if rule else None
        rows.append(row)
    return {'count': total, 'page': max(number, 1), 'page_size': PAGE_SIZE, 'results': rows}


def rule_json(rule):
    return {'id': rule.pk, 'strategy': rule.strategy, 'step': str(rule.step), 'min_price': str(rule.min_price),
            'max_price': str(rule.max_price), 'is_active': rule.is_active,
            'last_run_at': rule.last_run_at.isoformat() if rule.last_run_at else None, 'last_result': rule.last_result}


# ── Repricer ─────────────────────────────────────────────────────────────────

def target_price(rule, current, lowest_other):
    """(new price, note). None price = leave it."""
    if lowest_other is None:
        target = rule.max_price          # nobody to compete with: earn the most you allow
        note = 'No other sellers in stock: at your maximum.'
    elif rule.strategy == rule.MATCH:
        target, note = lowest_other, f'Matching the cheapest seller (₵{lowest_other:.2f}).'
    else:
        target, note = lowest_other - rule.step, f'Beating the cheapest seller (₵{lowest_other:.2f}) by ₵{rule.step:.2f}.'
    if target < rule.min_price:
        target, note = rule.min_price, (f'Holding at your minimum ₵{rule.min_price:.2f}; '
                                        f'the cheapest seller is ₵{lowest_other:.2f}.')
    target = min(max(target, rule.min_price), rule.max_price).quantize(Decimal('0.01'))
    return (None if target == current else target), note


def run_repricer(now=None):
    """Apply every due, active rule of stores whose plan includes the repricer. Returns prices changed."""
    from notification.utils import send_notification
    from payments.entitlements import plan_for_vendor
    from tools import bulk_edit, registry
    from tools.models import PriceChange, RepricerRule

    now = now or timezone.now()
    due = list(RepricerRule.objects.filter(is_active=True, product__status='published',
                                           product__catalog_item__isnull=False)
               .select_related('product', 'vendor', 'vendor__user')
               .order_by('last_run_at')[:REPRICER_BATCH])
    allowed = {}
    for rule in due:
        if rule.vendor_id not in allowed:
            allowed[rule.vendor_id] = registry.included(plan_for_vendor(rule.vendor), 'auto_repricer')
    due = [r for r in due if allowed[r.vendor_id]]
    offers = competing_offers({r.product.catalog_item_id for r in due})

    changed_products, logs = [], []
    for rule in due:
        p = rule.product
        others = [price for pid, vid, price in offers.get(p.catalog_item_id, []) if vid != rule.vendor_id]
        lowest = min(others) if others else None
        new_price, note = target_price(rule, p.price, lowest)
        rule.last_run_at, rule.last_result = now, note[:160]
        if new_price is not None:
            logs.append(PriceChange(vendor_id=rule.vendor_id, product=p, old_price=p.price, new_price=new_price,
                                    source=PriceChange.REPRICER, note=note[:160]))
            p.price = new_price
            changed_products.append(p)
        if lowest is not None and lowest < rule.min_price and rule.vendor.user_id \
                and cache.add(f'tools:repricer_min:{rule.pk}', 1, AT_MINIMUM_NOTICE):
            send_notification(rule.vendor.user, 'vendor_repricer_at_minimum', data={
                'message': f'{p.title}: another seller is at ₵{lowest:.2f}, below your minimum of '
                           f'₵{rule.min_price:.2f}. Your price stays at ₵{rule.min_price:.2f}.',
                'url': '/tools/pricing'})

    with transaction.atomic():
        if changed_products:
            Product.objects.bulk_update(changed_products, ['price'], batch_size=500)
            PriceChange.objects.bulk_create(logs, batch_size=500)
            bulk_edit._after_save({p.pk for p in changed_products})
        if due:
            RepricerRule.objects.bulk_update(due, ['last_run_at', 'last_result'], batch_size=500)
    return len(changed_products)

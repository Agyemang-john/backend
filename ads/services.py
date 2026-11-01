"""
Ad serving, click billing, stats buffering, nightly billing and attribution.

Serving (per listing request, results cached briefly per context):
  1. candidates  – keyword match: the query's 1-4 word n-grams looked up by
                   index (AdKeyword.phrase IN ...); auto targeting: advertised
                   products whose search vector matches; categories: same
                   sub-category; brand / collection / deals pages: advertised
                   products inside that page's own product set (`scope`). Only live campaigns of funded stores, products
                   that are published, featured and in stock.
  2. rank        – bid × quality (smoothed CTR vs. baseline, clipped 0.5-2).
  3. price       – second price: just enough to beat the next ad, at least
                   RESERVE_CPC, never above the bid. Signed into the click token.
Impressions go to a Redis hash, flushed every few minutes; clicks are
validated, de-duplicated per visitor and written straight away.
"""
import hashlib
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from .models import AdCampaign, AdCampaignProduct, AdClick, AdDailyStat, AdKeyword

logger = logging.getLogger(__name__)

RESERVE_CPC = Decimal(str(getattr(settings, 'ADS_RESERVE_CPC', '0.20')))
MIN_BID = RESERVE_CPC
SLOTS = 2
BASELINE_CTR = 0.02
TOKEN_SALT = 'ads.click'
TOKEN_MAX_AGE = 3600
CLICK_DEDUP_SECONDS = 30 * 60
SERVE_CACHE_SECONDS = 60
ATTRIBUTION_DAYS = 7
CENT = Decimal('0.01')


def _q(v):
    return Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


def normalise(text):
    return ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in (text or '').lower()).split())[:200]


def ngrams(query, max_n=4):
    words = normalise(query).split()[:12]
    out = set()
    for n in range(1, max_n + 1):
        for i in range(len(words) - n + 1):
            out.add(' '.join(words[i:i + n]))
    return out


# ── Funding: ads are paid from earnings ──────────────────────────────────────

FUNDED_KEY = 'ads:funded_vendors'


def funded_vendor_ids():
    ids = cache.get(FUNDED_KEY)
    if ids is None:
        ids = refresh_funded_vendors()
    return ids


def refresh_funded_vendors():
    """
    Stores that can pay for clicks: earnings (available + pending) above their
    not-yet-billed spend, or a saved card that nightly billing charges for any
    shortfall (ads/billing.py) — so new stores without earnings can advertise.
    """
    from payments.ledger_models import LedgerEntry
    vendor_ids = set(AdCampaign.objects.filter(status=AdCampaign.ACTIVE).values_list('vendor_id', flat=True))
    if not vendor_ids:
        cache.set(FUNDED_KEY, set(), 600)
        return set()
    balances = dict(LedgerEntry.objects.filter(vendor_id__in=vendor_ids, payout__isnull=True)
                    .values_list('vendor_id').annotate(b=Sum('amount')))
    unbilled = dict(AdDailyStat.objects.filter(campaign__vendor_id__in=vendor_ids, billed=False)
                    .values_list('campaign__vendor_id').annotate(s=Sum('spend')))
    credit = Decimal(str(getattr(settings, 'ADS_CREDIT_LIMIT', '0')))
    from .billing import card_paused, vendors_with_cards
    funded = {v for v in vendor_ids
              if (balances.get(v) or 0) - (unbilled.get(v) or 0) + credit > 0 and not card_paused(v)}
    funded |= vendors_with_cards(vendor_ids - funded)
    cache.set(FUNDED_KEY, funded, 600)
    return funded


# ── Spend today (budget pacing) ──────────────────────────────────────────────

def _spend_key(campaign_id, day=None):
    return f"ads:spend:{(day or timezone.localdate()).isoformat()}:{campaign_id}"


def spend_today(campaign_ids):
    keys = {cid: _spend_key(cid) for cid in campaign_ids}
    cached = cache.get_many(list(keys.values()))
    out, missing = {}, []
    for cid, key in keys.items():
        if key in cached:
            out[cid] = Decimal(str(cached[key]))
        else:
            missing.append(cid)
    if missing:
        today = timezone.localdate()
        db = dict(AdDailyStat.objects.filter(campaign_id__in=missing, date=today)
                  .values_list('campaign_id').annotate(s=Sum('spend')))
        for cid in missing:
            out[cid] = db.get(cid) or Decimal('0')
            cache.set(keys[cid], str(out[cid]), 86400)
    return out


# ── Serving ──────────────────────────────────────────────────────────────────

def _live_campaigns():
    today = timezone.localdate()
    return (AdCampaign.objects.filter(status=AdCampaign.ACTIVE, start_date__lte=today)
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today)))


def _quality(pairs):
    """{(campaign_id, product_id): factor} from the last 30 days, smoothed toward the baseline."""
    if not pairs:
        return {}
    since = timezone.localdate() - timedelta(days=30)
    rows = (AdDailyStat.objects.filter(date__gte=since, campaign_id__in={c for c, _ in pairs},
                                       product_id__in={p for _, p in pairs})
            .values_list('campaign_id', 'product_id').annotate(i=Sum('impressions'), c=Sum('clicks')))
    stats = {(c, p): (i, k) for c, p, i, k in rows}
    out = {}
    for pair in pairs:
        imps, clicks = stats.get(pair, (0, 0))
        ctr = (clicks + BASELINE_CTR * 100) / (imps + 100)      # 100 virtual impressions at baseline
        out[pair] = min(2.0, max(0.5, ctr / BASELINE_CTR))
    return out


def _candidates(placement, query=None, sub_category_id=None, scope=None):
    """
    [(campaign, product_id, bid)] eligible for this placement. `scope` (a Q on
    Product, or an iterable of product ids) limits browse placements to the
    page's own products: a brand page only advertises that brand.
    """
    from product.models import Product

    live = _live_campaigns().filter(vendor_id__in=funded_vendor_ids())
    found = {}   # (campaign_id, product_id) -> bid

    if placement == 'search':
        grams = ngrams(query)
        full = normalise(query)
        if grams:
            for kw in AdKeyword.objects.filter(campaign__in=live, phrase__in=grams).select_related('campaign'):
                if kw.match == AdKeyword.EXACT and kw.phrase != full:
                    continue
                bid = kw.bid or kw.campaign.bid
                for pid in AdCampaignProduct.objects.filter(campaign_id=kw.campaign_id).values_list('product_id', flat=True):
                    key = (kw.campaign_id, pid)
                    found[key] = max(found.get(key, Decimal('0')), bid)
        if full:
            from django.contrib.postgres.search import SearchQuery
            from product.models import SEARCH_CONFIG
            matches = (Product.objects.filter(search_vector=SearchQuery(full, config=SEARCH_CONFIG),
                                              ad_items__campaign__in=live.filter(auto_targeting=True))
                       .values_list('ad_items__campaign_id', 'pk', 'ad_items__campaign__bid')[:200])
            for cid, pid, bid in matches:
                found.setdefault((cid, pid), bid)
    else:
        items = AdCampaignProduct.objects.filter(campaign__in=live)
        if placement == 'category':
            items = items.filter(product__sub_category_id=sub_category_id)
        elif isinstance(scope, Q):
            items = items.filter(product__in=Product.objects.filter(scope))
        elif scope is not None:
            items = items.filter(product_id__in=list(scope)[:5000])
        else:
            return []
        for cid, pid, bid in items.values_list('campaign_id', 'product_id', 'campaign__bid')[:200]:
            found.setdefault((cid, pid), bid)
    if not found:
        return []

    # Product must be buyable and the store's listing the featured one.
    pids = {p for _, p in found}
    ok = set(Product.published.filter(pk__in=pids, is_buybox_winner=True)
             .filter(Q(variant='None', total_quantity__gt=0) | ~Q(variant='None'))
             .values_list('pk', flat=True))
    spend = spend_today({c for c, _ in found})
    budgets = dict(AdCampaign.objects.filter(pk__in={c for c, _ in found}).values_list('pk', 'daily_budget'))
    return [(cid, pid, bid) for (cid, pid), bid in found.items()
            if pid in ok and spend.get(cid, 0) + bid <= budgets.get(cid, 0)]


def auction(candidates, slots=SLOTS):
    """Pick winners and second prices. One product appears once."""
    q = _quality([(c, p) for c, p, _ in candidates])
    scored = sorted(((float(bid) * q[(c, p)], c, p, bid) for c, p, bid in candidates), reverse=True)
    winners, seen = [], set()
    for i, (score, c, p, bid) in enumerate(scored):
        if p in seen:
            continue
        seen.add(p)
        nxt = next((s for s, _, np, _ in scored[i + 1:] if np not in seen), None)
        price = RESERVE_CPC if nxt is None else Decimal(str(nxt / q[(c, p)])) + CENT
        winners.append({'campaign_id': c, 'product_id': p, 'cost': _q(min(max(price, RESERVE_CPC), bid))})
        if len(winners) == slots:
            break
    return winners


def sponsored_products(placement, query=None, sub_category_id=None, exclude_ids=(), scope=None, scope_key=''):
    """
    [(product, ad_token)] winning this placement, impressions recorded. Never raises.

    Placements: 'search' (query), 'category' (sub_category_id), and 'brand',
    'collection', 'deals', which pass `scope` (see _candidates) plus a short
    `scope_key` naming it (brand id, collection slug) for the serve cache.
    """
    from product.models import Product
    try:
        key = (f"ads:serve:{placement}:{hashlib.md5(normalise(query or '').encode()).hexdigest()}"
               f":{sub_category_id}:{scope_key}")
        winners = cache.get(key)
        if winners is None:
            winners = auction(_candidates(placement, query, sub_category_id, scope))
            cache.set(key, winners, SERVE_CACHE_SECONDS)
        winners = [w for w in winners if w['product_id'] not in set(exclude_ids)]
        if not winners:
            return []
        products = {p.pk: p for p in Product.objects.filter(pk__in=[w['product_id'] for w in winners])
                    .select_related('brand', 'vendor', 'sub_category')}
        out = []
        for w in winners:
            p = products.get(w['product_id'])
            if p is None:
                continue
            token = signing.dumps({'c': w['campaign_id'], 'p': p.pk, 'k': str(w['cost']), 'pl': placement},
                                  salt=TOKEN_SALT, compress=True)
            out.append((p, token))
            record_impression(w['campaign_id'], p.pk)
        return out
    except Exception:  # noqa: BLE001 - ads must never break a listing
        logger.exception("sponsored products failed for %s", placement)
        return []


def sponsored(request, placement, **kwargs):
    """Sponsored rows in the listing-grid shape (products_with_details + 'sponsored' and 'ad_token')."""
    from product.cards import listing_rows
    try:
        won = sponsored_products(placement, **kwargs)
        rows = listing_rows([p for p, _ in won], request)
        for row, (_, token) in zip(rows, won):
            row['sponsored'], row['ad_token'] = True, token
        return rows
    except Exception:  # noqa: BLE001
        logger.exception("sponsored rows failed for %s", placement)
        return []


# ── Impressions (buffered) ───────────────────────────────────────────────────

IMP_HASH = 'ads:imp'


def _redis():
    try:
        from django_redis import get_redis_connection
        return get_redis_connection('default')
    except Exception:  # noqa: BLE001
        return None


def record_impression(campaign_id, product_id):
    conn = _redis()
    field = f"{timezone.localdate().isoformat()}:{campaign_id}:{product_id}"
    if conn is not None:
        try:
            conn.hincrby(IMP_HASH, field, 1)
            return
        except Exception:  # noqa: BLE001
            pass
    _add_stat(campaign_id, product_id, timezone.localdate(), impressions=1)


def flush_impressions():
    conn = _redis()
    if conn is None:
        return 0
    tmp = f"{IMP_HASH}:flushing"
    try:
        if not conn.exists(IMP_HASH):
            return 0
        conn.rename(IMP_HASH, tmp)
        data = conn.hgetall(tmp)
        conn.delete(tmp)
    except Exception:  # noqa: BLE001
        logger.exception("ads impression flush failed")
        return 0
    n = 0
    for raw, count in data.items():
        day, cid, pid = (raw.decode() if isinstance(raw, bytes) else raw).split(':')
        _add_stat(int(cid), int(pid), date.fromisoformat(day), impressions=int(count))
        n += 1
    return n


def _add_stat(campaign_id, product_id, day, impressions=0, clicks=0, spend=Decimal('0')):
    updated = AdDailyStat.objects.filter(campaign_id=campaign_id, product_id=product_id, date=day).update(
        impressions=F('impressions') + impressions, clicks=F('clicks') + clicks, spend=F('spend') + spend)
    if not updated:
        try:
            with transaction.atomic():
                AdDailyStat.objects.create(campaign_id=campaign_id, product_id=product_id, date=day,
                                           impressions=impressions, clicks=clicks, spend=spend)
        except IntegrityError:
            _add_stat(campaign_id, product_id, day, impressions, clicks, spend)


# ── Clicks ───────────────────────────────────────────────────────────────────

def visitor_key(request):
    from core.client_ip import client_ip
    from product.utils import _get_visitor_id
    vid = _get_visitor_id(request)
    if vid is None:
        raw = f"{client_ip(request)}|{request.headers.get('User-Agent', '')[:200]}"
        vid = 'h:' + hashlib.sha1(raw.encode()).hexdigest()
    return vid[:40]


def record_click(request, token):
    """Returns (status, cost_or_None). Status: billed | duplicate | invalid | bot | budget."""
    from product.utils import detect_bot
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        cid, pid, cost, placement = int(data['c']), int(data['p']), Decimal(data['k']), data['pl']
    except (signing.BadSignature, KeyError, ValueError, TypeError, ArithmeticError):
        return 'invalid', None
    if detect_bot(request):
        return 'bot', None
    visitor = visitor_key(request)
    if not cache.add(f"ads:clk:{visitor}:{cid}:{pid}", 1, CLICK_DEDUP_SECONDS):
        return 'duplicate', None
    campaign = AdCampaign.objects.filter(pk=cid).only('daily_budget', 'vendor_id').first()
    if campaign is None:
        return 'invalid', None
    if campaign.vendor_id and request.user.is_authenticated and \
            AdCampaign.objects.filter(pk=cid, vendor__members__user=request.user, vendor__members__is_active=True).exists():
        return 'duplicate', None     # store staff clicking their own ads aren't billed
    spent = spend_today([cid]).get(cid, Decimal('0'))
    if spent + cost > campaign.daily_budget:
        return 'budget', None
    AdClick.objects.create(campaign_id=cid, product_id=pid, cost=cost, placement=placement[:10], visitor=visitor,
                           user=request.user if request.user.is_authenticated else None)
    _add_stat(cid, pid, timezone.localdate(), clicks=1, spend=cost)
    cache.set(_spend_key(cid), str(spent + cost), 86400)
    return 'billed', cost


# ── Nightly: billing + attribution ───────────────────────────────────────────

def bill_spend(today=None):
    """Post every store's unbilled spend for days before today as one ledger debit per store and day."""
    from payments.ledger_models import LedgerEntry
    today = today or timezone.localdate()
    rows = (AdDailyStat.objects.filter(billed=False, date__lt=today, spend__gt=0)
            .values_list('campaign__vendor_id', 'date').annotate(total=Sum('spend')))
    posted, billed_vendors = 0, set()
    for vendor_id, day, total in rows:
        with transaction.atomic():
            stats = AdDailyStat.objects.select_for_update().filter(campaign__vendor_id=vendor_id, date=day, billed=False)
            amount = stats.aggregate(s=Sum('spend'))['s'] or Decimal('0')
            if amount <= 0:
                continue
            LedgerEntry.objects.create(vendor_id=vendor_id, entry_type=LedgerEntry.AD_SPEND, amount=-_q(amount),
                                       description=f"Sponsored products, {day:%d %b %Y}", available_at=timezone.now())
            stats.update(billed=True)
            posted += 1
            billed_vendors.add(vendor_id)
    AdDailyStat.objects.filter(billed=False, date__lt=today, spend=0).update(billed=True)
    # Earnings didn't cover it: charge the saved card for the difference.
    from vendor.models import Vendor

    from .billing import settle_with_card
    for vendor in Vendor.objects.filter(pk__in=billed_vendors).select_related('user'):
        settle_with_card(vendor)
    refresh_funded_vendors()
    return posted


def attribute_orders(since=None):
    """Order lines bought within ATTRIBUTION_DAYS of a click on that product by the same shopper."""
    from order.models import OrderProduct
    since = since or timezone.now() - timedelta(days=2)
    lines = (OrderProduct.objects.filter(order__is_ordered=True, order__date_created__gte=since, product__isnull=False)
             .exclude(order__user__isnull=True).select_related('order').only('pk', 'product_id', 'amount', 'order__user_id', 'order__date_created'))
    n = 0
    for line in lines.iterator(chunk_size=500):
        ordered_at = line.order.date_created
        click = (AdClick.objects.filter(user_id=line.order.user_id, product_id=line.product_id, order_product__isnull=True,
                                        created_at__lte=ordered_at,
                                        created_at__gte=ordered_at - timedelta(days=ATTRIBUTION_DAYS))
                 .order_by('-created_at').first())
        if click is None or AdClick.objects.filter(order_product=line).exists():
            continue
        AdClick.objects.filter(pk=click.pk).update(order_product=line)
        AdDailyStat.objects.filter(campaign_id=click.campaign_id, product_id=click.product_id,
                                   date=timezone.localdate(click.created_at)).update(
            orders=F('orders') + 1, sales=F('sales') + line.amount)
        n += 1
    return n


def prune_clicks(days=90):
    return AdClick.objects.filter(created_at__lt=timezone.now() - timedelta(days=days)).delete()[0]

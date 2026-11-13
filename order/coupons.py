"""
order/coupons.py
Coupon rules and maths, shared by checkout, pay-on-delivery and the Paystack check.

Who pays the discount
    Negromart coupon (Coupon.vendor empty)  any store's items; sellers are paid
                                            the full price, Negromart absorbs it.
    Store coupon (Coupon.vendor set)        that store's items only; the discount
                                            comes off the store's earnings
                                            (payments/ledger.py SELLER_DISCOUNT).

How a shopper gets a discount (Amazon-style)
    Clipped coupons  Coupon.clippable shows a "Save 10% with coupon" badge on the
                     products it covers (offers_for_products). The shopper clips
                     it (ClippedCoupon) and it applies by itself at checkout.
                     Several can apply to one order, but each item gets at most
                     one clipped coupon: the biggest saving wins.
    One typed code   CartCouponView; applied on top, to what's left after clips.

Rules, in the order the shopper hears about them
    switched on -> inside its dates -> total uses (max_uses) -> budget ->
    uses per shopper (per_user_limit) -> applies to something in the cart
    (store / products / categories) -> minimum spend on those items.

Maths
    Fixed amount (never more than the eligible items) or a percentage of the
    eligible items (optionally capped, and never more than the budget left).
    Item prices only, never delivery. Each coupon is split across its lines pro
    rata, to the cent, so refunds and seller earnings work per line.

strict=False is used when a card payment is being verified: the shopper has
already paid the discounted price, so the use, budget and per-shopper checks
are skipped and a coupon that ended in the last couple of hours still counts.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.core.cache import cache
from django.db.models import F
from django.utils import timezone

from product.models import ClippedCoupon, Coupon, CouponRedemption, Product

CENT = Decimal('0.01')
ZERO = Decimal('0')
PAYMENT_GRACE = timedelta(hours=2)      # card payment started before the coupon ended
OFFERS_CACHE_KEY = 'coupon_offers:v1'
OFFERS_TTL = 60


class CouponError(Exception):
    def __init__(self, code, message, **params):
        super().__init__(message)
        self.code = code
        self.message = message
        self.params = params   # values in the message, so the storefront can translate it


@dataclass
class Quote:
    """One coupon on the cart."""
    coupon: object = None
    discount: Decimal = ZERO
    shares: dict = field(default_factory=dict)  # line key -> Decimal share of the discount
    error: CouponError = None

    @property
    def ok(self):
        return self.coupon is not None and self.error is None

    @property
    def seller_funded(self):
        return bool(self.coupon is not None and self.coupon.vendor_id)

    def seller_share(self, key):
        return self.shares.get(key, ZERO) if self.seller_funded else ZERO


@dataclass
class CartDeal:
    """Everything taken off a cart: its clipped coupons plus the typed code."""
    clips: list = field(default_factory=list)   # applied clipped coupons (ok Quotes)
    code: Quote = field(default_factory=Quote)  # the typed code; may carry an error

    @property
    def applied(self):
        return self.clips + ([self.code] if self.code.ok else [])

    @property
    def discount(self):
        return sum((q.discount for q in self.applied), ZERO)

    def share(self, key):
        return sum((q.shares.get(key, ZERO) for q in self.applied), ZERO)

    def seller_share(self, key):
        return sum((q.seller_share(key) for q in self.applied), ZERO)

    @property
    def code_broken(self):
        """A typed code is attached but no longer works."""
        return self.code.coupon is not None and not self.code.ok

    def redemptions(self):
        """[{'id', 'discount'}] for redeem() and the payment task."""
        return [{'id': q.coupon.pk, 'discount': str(q.discount)} for q in self.applied]


def find_coupon(code):
    code = (code or '').strip().upper()
    if not code:
        raise CouponError('missing', "Enter a coupon code.")
    coupon = Coupon.objects.select_related('vendor').filter(code=code).first()
    if coupon is None:
        raise CouponError('not_found', "That code isn't valid.")
    return coupon


def check_live(coupon, user, strict=True):
    """The checks that don't depend on the cart. Raises CouponError."""
    if not coupon.active:
        raise CouponError('inactive', "This coupon is no longer available.")
    now = timezone.now()
    if strict:
        if now < coupon.valid_from:
            raise CouponError('not_started', f"This coupon starts on {coupon.valid_from:%d %b %Y}.",
                              date=coupon.valid_from.isoformat())
        if now > coupon.valid_to:
            raise CouponError('expired', "This coupon has expired.")
        if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
            raise CouponError('used_up', "This coupon has been fully used.")
        if coupon.budget is not None and coupon.budget_left <= 0:
            raise CouponError('used_up', "This coupon has been fully used.")
        if user is not None and getattr(user, 'is_authenticated', True) and coupon.per_user_limit is not None:
            used = CouponRedemption.objects.filter(coupon=coupon, user=user).count()
            if used >= coupon.per_user_limit:
                raise CouponError('already_used', "You've already used this coupon.")
    elif now > coupon.valid_to + PAYMENT_GRACE:
        raise CouponError('expired', "This coupon has expired.")


def evaluate(coupon, user, lines, strict=True):
    """
    lines: [(key, product, amount)] with amount = what the line costs now
    (flash prices included, earlier coupons taken off). Returns a Quote or
    raises CouponError.
    """
    check_live(coupon, user, strict=strict)

    product_ids = set(coupon.applicable_products.values_list('id', flat=True))
    category_ids = set(coupon.applicable_categories.values_list('id', flat=True))

    def applies(product):
        if coupon.vendor_id and product.vendor_id != coupon.vendor_id:
            return False
        if not product_ids and not category_ids:
            return True
        # Products sit in a sub-category, which belongs to a category.
        category_id = getattr(product.sub_category, 'category_id', None) if product.sub_category_id else None
        return product.id in product_ids or (category_id is not None and category_id in category_ids)

    eligible = [(key, product, Decimal(amount)) for key, product, amount in lines
                if product is not None and Decimal(amount) > 0 and applies(product)]
    subtotal = sum((amount for _, _, amount in eligible), ZERO)
    if not eligible or subtotal <= 0:
        if coupon.vendor_id and coupon.vendor:
            raise CouponError('not_applicable', f"This coupon is for items from {coupon.vendor.name} only.",
                              store=coupon.vendor.name)
        raise CouponError('not_applicable', "This coupon doesn't apply to the items in your cart.")

    minimum = coupon.min_purchase_amount
    if minimum and subtotal < minimum:
        raise CouponError(
            'min_purchase',
            f"Spend GHS {minimum - subtotal:,.2f} more on eligible items to use this coupon.",
            amount=f"{minimum - subtotal:,.2f}",
        )

    if coupon.is_percentage:
        discount = subtotal * Decimal(str(coupon.discount_percentage)) / 100
        if coupon.max_discount_amount:
            discount = min(discount, Decimal(coupon.max_discount_amount))
    else:
        discount = Decimal(coupon.discount_amount or 0)
    if strict and coupon.budget is not None:
        discount = min(discount, coupon.budget_left)
    discount = min(discount, subtotal).quantize(CENT, rounding=ROUND_HALF_UP)
    if discount <= 0:
        raise CouponError('not_applicable', "This coupon doesn't apply to the items in your cart.")

    return Quote(coupon=coupon, discount=discount, shares=_split(discount, eligible, subtotal))


def _split(total, eligible, subtotal):
    """Pro-rata shares that add up to `total` exactly (leftover cents to the biggest lines)."""
    shares = {}
    given = ZERO
    for key, _, amount in eligible:
        share = (total * amount / subtotal).quantize(CENT, rounding=ROUND_DOWN)
        shares[key] = share
        given += share
    left = total - given
    for key, _, amount in sorted(eligible, key=lambda line: -line[2]):
        if left <= 0:
            break
        if shares[key] + CENT <= amount:
            shares[key] += CENT
            left -= CENT
    return shares


def cart_lines(cart):
    return [
        (item.pk, item.product, item.amount)
        for item in cart.cart_items.select_related('product__sub_category').all()
    ]


def _locked(qs, lock):
    # Row lock so concurrent orders can't push used_count/budget past their limits.
    return qs.select_for_update(of=('self',)) if lock else qs


def _apply_clips(user, lines, strict, lock, skip_id=None):
    """Best clipped coupon per item: biggest saving first, each item used once."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return []
    qs = (Coupon.objects.select_related('vendor')
          .filter(clips__user=user, clippable=True, active=True).order_by('pk'))
    if skip_id:
        qs = qs.exclude(pk=skip_id)
    candidates = []
    for coupon in _locked(qs, lock):
        try:
            candidates.append(evaluate(coupon, user, lines, strict=strict))
        except CouponError:
            continue
    candidates.sort(key=lambda q: (-q.discount, q.coupon.pk))

    taken, applied = set(), []
    for candidate in candidates:
        left = [line for line in lines if line[0] not in taken]
        try:
            quote = evaluate(candidate.coupon, user, left, strict=strict) if taken else candidate
        except CouponError:
            continue
        applied.append(quote)
        taken |= {key for key, share in quote.shares.items() if share > 0}
    return applied


def quote_cart(cart, strict=True, lock=False):
    """Clipped coupons, then the typed code on what's left. Never raises."""
    lines = cart_lines(cart)
    clips = _apply_clips(cart.user, lines, strict, lock, skip_id=cart.coupon_id)

    code = Quote()
    if cart.coupon_id:
        coupon = _locked(Coupon.objects.select_related('vendor'), lock).filter(pk=cart.coupon_id).first()
        if coupon is not None:
            taken = {}
            for q in clips:
                for key, share in q.shares.items():
                    taken[key] = taken.get(key, ZERO) + share
            net = [(key, product, Decimal(amount) - taken.get(key, ZERO)) for key, product, amount in lines]
            try:
                code = evaluate(coupon, cart.user, net, strict=strict)
            except CouponError as exc:
                code = Quote(coupon=coupon, error=exc)
    return CartDeal(clips=clips, code=code)


def apply_to_cart(cart, code):
    """Check the code against the cart now and attach it. Raises CouponError."""
    coupon = find_coupon(code)
    cart.coupon = coupon
    deal = quote_cart(cart)
    if deal.code.error:
        raise deal.code.error
    cart.save(update_fields=['coupon', 'updated_at'])
    return deal.code


def remove_from_cart(cart):
    if cart.coupon_id:
        cart.coupon = None
        cart.save(update_fields=['coupon', 'updated_at'])


def redeem(order, coupons, code_id=None):
    """
    Record the coupons used on a placed order; call inside its transaction.
    coupons: [{'id', 'discount'}] (CartDeal.redemptions()). code_id is the
    typed code among them, kept on Order.coupon.
    """
    total = ZERO
    for item in coupons or []:
        discount = Decimal(str(item.get('discount') or 0))
        if discount <= 0:
            continue
        coupon = Coupon.objects.select_for_update().filter(pk=item.get('id')).first()
        if coupon is None:
            continue
        _, created = CouponRedemption.objects.get_or_create(
            order=order, coupon=coupon, defaults={'user': order.user, 'amount': discount},
        )
        if created:
            Coupon.objects.filter(pk=coupon.pk).update(
                used_count=F('used_count') + 1, discount_spent=F('discount_spent') + discount,
            )
        total += discount
    if total <= 0:
        return
    if code_id and any(str(item.get('id')) == str(code_id) for item in coupons):
        order.coupon_id = code_id
    order.discount_total = total
    order.save(update_fields=['coupon', 'discount_total'])
    forget_offers()


# -- Badges: which products show "Save 10% with coupon" ------------------------

def forget_offers():
    try:
        cache.delete(OFFERS_CACHE_KEY)
    except Exception:  # noqa: BLE001 - cache down: next read rebuilds
        pass


def _live_clippable():
    """Clip coupons that can be used right now, cached for a minute."""
    try:
        rows = cache.get(OFFERS_CACHE_KEY)
    except Exception:  # noqa: BLE001
        rows = None
    if rows is not None:
        return rows
    now = timezone.now()
    rows = []
    qs = (Coupon.objects.filter(clippable=True, active=True, valid_from__lte=now, valid_to__gte=now)
          .select_related('vendor').prefetch_related('applicable_products', 'applicable_categories'))
    for c in qs:
        if c.max_uses is not None and c.used_count >= c.max_uses:
            continue
        if c.budget is not None and c.budget_left <= 0:
            continue
        rows.append({
            'id': c.pk,
            'vendor_id': c.vendor_id,
            'store': c.vendor.name if c.vendor_id and c.vendor else None,
            'percent': c.discount_percentage if c.is_percentage else None,
            'amount': str(c.discount_amount) if not c.is_percentage else None,
            'max_off': str(c.max_discount_amount) if c.is_percentage and c.max_discount_amount else None,
            'min_spend': str(c.min_purchase_amount) if c.min_purchase_amount else None,
            'valid_to': c.valid_to.isoformat(),
            'products': [p.pk for p in c.applicable_products.all()],
            'categories': [cat.pk for cat in c.applicable_categories.all()],
        })
    try:
        cache.set(OFFERS_CACHE_KEY, rows, OFFERS_TTL)
    except Exception:  # noqa: BLE001
        pass
    return rows


def _offer_value(offer, price):
    if offer['percent']:
        value = price * Decimal(str(offer['percent'])) / 100
        if offer['max_off']:
            value = min(value, Decimal(offer['max_off']))
        return value
    return min(Decimal(offer['amount'] or 0), price)


OFFER_FIELDS = ('id', 'store', 'percent', 'amount', 'max_off', 'min_spend', 'valid_to')


def offers_for_products(product_ids):
    """{product_id: offer} - the best clip coupon on each product, for its badge."""
    ids = [int(i) for i in dict.fromkeys(product_ids) if i]
    live = _live_clippable()
    if not ids or not live:
        return {}
    out = {}
    for pid, vendor_id, category_id, price in (Product.objects.filter(pk__in=ids)
                                               .values_list('pk', 'vendor_id', 'sub_category__category_id', 'price')):
        price = Decimal(price or 0)
        best, best_value = None, ZERO
        for offer in live:
            if offer['vendor_id'] and offer['vendor_id'] != vendor_id:
                continue
            if (offer['products'] or offer['categories']) and pid not in offer['products'] \
                    and category_id not in offer['categories']:
                continue
            value = _offer_value(offer, price)
            if value > best_value:
                best, best_value = offer, value
        if best:
            out[pid] = {k: best[k] for k in OFFER_FIELDS}
    return out


def clip(user, coupon_id):
    """Shopper clips a badge. Raises CouponError if it can't be used."""
    coupon = Coupon.objects.select_related('vendor').filter(pk=coupon_id, clippable=True).first()
    if coupon is None:
        raise CouponError('not_found', "This coupon isn't available.")
    check_live(coupon, user)
    ClippedCoupon.objects.get_or_create(user=user, coupon=coupon)
    return coupon


def unclip(user, coupon_id):
    ClippedCoupon.objects.filter(user=user, coupon_id=coupon_id).delete()


def clipped_ids(user):
    return list(ClippedCoupon.objects.filter(user=user).values_list('coupon_id', flat=True))


# -- Checkout summary ----------------------------------------------------------

def describe(quote):
    """Shopper-facing block for one coupon on the cart."""
    if quote.coupon is None:
        return None
    coupon = quote.coupon
    return {
        'id': coupon.pk,
        'code': coupon.code,
        'store': coupon.vendor.name if coupon.vendor_id and coupon.vendor else None,
        'percentage': coupon.discount_percentage if coupon.is_percentage else None,
        'amount_off': str(coupon.discount_amount) if not coupon.is_percentage else None,
        'valid_to': coupon.valid_to,
        'discount': str(quote.discount) if quote.ok else '0.00',
        'error': quote.error.message if quote.error else None,
        'error_code': quote.error.code if quote.error else None,
        'error_params': quote.error.params if quote.error else None,
    }


def describe_deal(deal, cart):
    """Summary fields: the typed code, the clipped coupons applied, and unclipped offers on cart items."""
    clipped = set(clipped_ids(cart.user)) if cart.user_id else set()
    offers = offers_for_products(cart.cart_items.values_list('product_id', flat=True))
    available = {}
    for offer in offers.values():
        if offer['id'] not in clipped and offer['id'] != cart.coupon_id:
            available[offer['id']] = offer
    return {
        'coupon': describe(deal.code),
        'clipped_coupons': [describe(q) for q in deal.clips],
        'coupons_available': list(available.values()),
    }

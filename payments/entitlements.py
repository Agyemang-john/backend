"""
payments/entitlements.py
What a store's subscription plan allows, in one place.

A store without a subscription in force (active, trial, or past due within
its grace period — see VendorSubscription.in_force) is on the cheapest active
'free' plan. If no plan exists at all (fresh install), the defaults below
apply so nothing crashes and nobody gets paid features for free.

Used by: commission and payout holds (payments/ledger.py), team size
(vendor/team_views.py), CSV exports, and payments/subscription_permissions.py.
"""

from decimal import Decimal

from django.conf import settings

from .models import SubscriptionPlan, VendorSubscription

# Fallbacks when no plan row exists. Commission matches the 20% the old
# batch payout hard-coded, so behaviour doesn't change silently.
DEFAULT_COMMISSION_RATE = Decimal(str(getattr(settings, 'DEFAULT_COMMISSION_RATE', '20.00')))
DEFAULT_PAYOUT_DELAY_DAYS = int(getattr(settings, 'DEFAULT_PAYOUT_DELAY_DAYS', 7))

_CACHE_ATTR = '_plan_cache'
_MISSING = object()


def live_subscription(vendor):
    """
    The subscription currently granting this store a plan, or None.

    Dates are checked here, not left to the expiry sweep: a plan that ended
    (or a trial, or a grace period that ran out) stops counting the moment it
    ends, even if the hourly sweep hasn't marked it expired yet.
    """
    if vendor is None:
        return None
    sub = (
        VendorSubscription.objects.select_related('plan')
        .filter(vendor=vendor, status__in=VendorSubscription.LIVE_STATUSES)
        .order_by('-created_at')
        .first()
    )
    return sub if sub and sub.in_force() else None


def free_plan():
    return SubscriptionPlan.objects.filter(tier='free', is_active=True).order_by('price').first()


def plan_for_vendor(vendor):
    """The plan in force for this store right now (memoised on the instance)."""
    if vendor is None:
        return None
    cached = vendor.__dict__.get(_CACHE_ATTR, _MISSING)
    if cached is not _MISSING:
        return cached
    sub = live_subscription(vendor)
    plan = sub.plan if sub else free_plan()
    vendor.__dict__[_CACHE_ATTR] = plan
    return plan


def forget_plan(vendor):
    """Drop the memoised plan after the store's subscription changed."""
    if vendor is not None:
        vendor.__dict__.pop(_CACHE_ATTR, None)


# ── Listing limit ─────────────────────────────────────────────────────────────
# The limit is checked against the real number of products the store has, not
# a counter: a counter drifts (it used to be reset to 0 on every plan change,
# which let a store add a fresh allowance each time) and can't see products
# created by other paths.

class ProductLimitReached(Exception):
    def __init__(self, limit, used, requested=1):
        self.limit, self.used, self.requested = limit, used, requested
        left = max(limit - used, 0)
        super().__init__(
            f"Your plan allows {limit} products and you have {used}. "
            + (f"You can add {left} more. " if left else "")
            + "Upgrade your plan to add more."
        )

    def payload(self, plan=None):
        return {
            "error":           "product_limit_reached",
            "detail":          str(self),
            "limit":           self.limit,
            "current_count":   self.used,
            "remaining_slots": max(self.limit - self.used, 0),
            "requested":       self.requested,
            "current_tier":    plan.tier if plan else "free",
            "action":          "upgrade",
            "upgrade_url":     "/subscribe",
        }


def product_count(vendor):
    from product.models import Product
    return Product.objects.filter(vendor=vendor).count()


def product_limit(vendor):
    """Max products for the store's plan right now (None = no plan configured, no limit)."""
    plan = plan_for_vendor(vendor)
    return plan.max_products if plan else None


def reserve_product_slots(vendor, requested=1):
    """
    Raise ProductLimitReached unless the store can add `requested` products.

    MUST run inside transaction.atomic() together with the product creation:
    the store row is locked, so two uploads at once can't both take the last
    slot. Returns (limit, used).
    """
    from django.db import transaction
    from vendor.models import Vendor
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("reserve_product_slots must run inside transaction.atomic()")
    Vendor.objects.select_for_update().only('pk').get(pk=vendor.pk)
    limit = product_limit(vendor)
    used = product_count(vendor)
    if limit is not None and used + requested > limit:
        raise ProductLimitReached(limit, used, requested)
    return limit, used


def sync_usage(vendor):
    """Keep SubscriptionUsage (shown on the billing page) equal to the real numbers."""
    from .models import SubscriptionUsage
    sub = live_subscription(vendor)
    usage, _ = SubscriptionUsage.objects.get_or_create(vendor=vendor)
    usage.subscription = sub
    usage.active_products_count = product_count(vendor)
    usage.period_end = sub.end_date if sub else None
    usage.save(update_fields=['subscription', 'active_products_count', 'period_end', 'updated_at'])
    return usage


def has_feature(vendor, flag):
    """True if the store's plan has boolean feature `flag` (e.g. 'can_export_reports')."""
    plan = plan_for_vendor(vendor)
    return bool(plan and getattr(plan, flag, False))


def commission_rate(vendor):
    plan = plan_for_vendor(vendor)
    return Decimal(plan.commission_rate) if plan else DEFAULT_COMMISSION_RATE


def payout_delay_days(vendor):
    plan = plan_for_vendor(vendor)
    return plan.payout_delay_days if plan else DEFAULT_PAYOUT_DELAY_DAYS


# Shopper ↔ seller chat (chat/) is a paid feature: Basic and above.
MESSAGING_MIN_TIER = 'basic'
_TIER_RANK = {'free': 0, 'basic': 1, 'pro': 2, 'enterprise': 3}


def can_use_messaging(vendor):
    plan = plan_for_vendor(vendor)
    tier = plan.tier if plan else 'free'
    return _TIER_RANK.get(tier, 0) >= _TIER_RANK[MESSAGING_MIN_TIER]


def team_member_limit(vendor):
    plan = plan_for_vendor(vendor)
    return plan.max_team_members if plan else 1


def variant_image_limit(vendor):
    """Extra gallery photos allowed per variant (0 = feature not in the plan)."""
    plan = plan_for_vendor(vendor)
    return plan.max_images_per_variant if plan else 0


def plan_summary(vendor):
    """Small dict for the frontend to grey out features with an upgrade hint."""
    plan = plan_for_vendor(vendor)
    return {
        'name': plan.name if plan else 'Free',
        'tier': plan.tier if plan else 'free',
        'can_export_reports': bool(plan and plan.can_export_reports),
        'can_offer_discounts': bool(plan and plan.can_offer_discounts),
        'can_use_messaging': can_use_messaging(vendor),
        'messaging_min_tier': MESSAGING_MIN_TIER,
        'max_team_members': team_member_limit(vendor),
        'max_images_per_variant': variant_image_limit(vendor),
        # Gallery photos per product (null = no plan, no limit), so the product
        # form can stop at the limit instead of failing on save.
        'max_images_per_product': plan.max_images_per_product if plan else None,
        'commission_rate': str(commission_rate(vendor)),
        'payout_delay_days': payout_delay_days(vendor),
        # Paid seller tools: which are included, and the cheapest plan that unlocks the others.
        'tools': _tools_summary(vendor),
    }


def _tools_summary(vendor):
    if vendor is None:
        return {}
    from tools.registry import summary
    return {t['key']: {k: v for k, v in t.items() if k not in ('key', 'summary')} for t in summary(vendor)}

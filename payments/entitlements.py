"""
payments/entitlements.py
What a store's subscription plan allows, in one place.

A store without an active (or trial) subscription is on the cheapest active
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


def plan_for_vendor(vendor):
    """The plan in force for this store right now (memoised on the instance)."""
    if vendor is None:
        return None
    cached = vendor.__dict__.get(_CACHE_ATTR, _MISSING)
    if cached is not _MISSING:
        return cached
    sub = (
        VendorSubscription.objects.select_related('plan')
        .filter(vendor=vendor, status__in=('active', 'trial'))
        .order_by('-created_at')
        .first()
    )
    plan = sub.plan if sub else (
        SubscriptionPlan.objects.filter(tier='free', is_active=True).order_by('price').first()
    )
    vendor.__dict__[_CACHE_ATTR] = plan
    return plan


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


def team_member_limit(vendor):
    plan = plan_for_vendor(vendor)
    return plan.max_team_members if plan else 1


def plan_summary(vendor):
    """Small dict for the frontend to grey out features with an upgrade hint."""
    plan = plan_for_vendor(vendor)
    return {
        'name': plan.name if plan else 'Free',
        'tier': plan.tier if plan else 'free',
        'can_export_reports': bool(plan and plan.can_export_reports),
        'max_team_members': team_member_limit(vendor),
        'commission_rate': str(commission_rate(vendor)),
        'payout_delay_days': payout_delay_days(vendor),
    }

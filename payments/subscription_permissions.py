# subscriptions/subscription_permissions.py
# ─────────────────────────────────────────────────────────────────────────────
# Drop-in DRF permission classes and a mixin for subscription enforcement.
#
# QUICK REFERENCE:
#
#   Tier gates (class-level, permission_classes):
#     RequireBasicPlan, RequireProPlan, RequireEnterprisePlan
#
#   Feature flag gates (inline factory):
#     require_feature("can_offer_discounts")
#     require_feature("can_access_bulk_upload")
#     require_feature("can_use_storefront_customization")
#     require_feature("can_feature_products")
#     require_feature("can_use_analytics")
#
#   Limit gates (mixin, fires inside perform_create/perform_update):
#     check_product_limit = True   → SubscriptionUsage.can_add_product()
#     check_image_limit   = True   → plan.max_images_per_product
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
from rest_framework.permissions import BasePermission
from rest_framework.exceptions import PermissionDenied
from django.db.models import F


from payments.models import VendorSubscription, SubscriptionUsage, SubscriptionPlan

TIER_ORDER: dict[str, int] = {"free": 0, "basic": 1, "pro": 2, "enterprise": 3}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _active_sub(user) -> VendorSubscription | None:
    try:
        return (
            VendorSubscription.objects
            .select_related("plan")
            .get(vendor__user=user, status__in=["active", "trial"])
        )
    except VendorSubscription.DoesNotExist:
        return None


def _plan(user) -> SubscriptionPlan | None:
    sub = _active_sub(user)
    if sub:
        return sub.plan
    return SubscriptionPlan.objects.filter(tier="free").order_by("price").first()


def _usage(user) -> SubscriptionUsage | None:
    vendor = getattr(user, 'vendor_user', None) or getattr(user, 'vendor', None)
    if not vendor:
        return None
    try:
        usage, _ = SubscriptionUsage.objects.get_or_create(
            vendor=vendor,
            defaults={'active_products_count': 0}
        )
        return usage
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[SubscriptionGate] _usage() failed for {vendor}: {e}")
        return None


def _upgrade_error(feature_label: str, current_tier: str, required_tier: str | None = None) -> dict:
    msg = f"Your current plan ({current_tier}) does not include {feature_label}."
    if required_tier:
        msg += f" Upgrade to {required_tier} or higher."
    return {
        "error": "plan_upgrade_required",
        "detail": msg,
        "current_tier": current_tier,
        "required_tier": required_tier,
        "action": "upgrade",
        "upgrade_url": "/subscribe",
    }


# ── Tier permissions ──────────────────────────────────────────────────────────

class _RequireMinTier(BasePermission):
    """Base — subclass and set min_tier."""
    min_tier: str = "free"

    def has_permission(self, request, view) -> bool:
        if not (request.user and request.user.is_authenticated):
            return False
        plan = _plan(request.user)
        vendor_rank = TIER_ORDER.get(plan.tier if plan else "free", 0)
        required_rank = TIER_ORDER.get(self.min_tier, 0)
        if vendor_rank < required_rank:
            self.message = _upgrade_error(
                f"this feature",
                plan.tier if plan else "free",
                self.min_tier,
            )
            return False
        return True


class RequireBasicPlan(_RequireMinTier):
    min_tier = "basic"

class RequireProPlan(_RequireMinTier):
    min_tier = "pro"

class RequireEnterprisePlan(_RequireMinTier):
    min_tier = "enterprise"


# ── Feature flag permission factory ──────────────────────────────────────────

def require_feature(flag: str) -> type[BasePermission]:
    """
    Returns a permission class that checks a boolean feature flag on the plan.

    Usage:
        permission_classes = [IsAuthenticated, IsVerifiedVendor, require_feature("can_offer_discounts")]
    """
    class _FeaturePermission(BasePermission):
        feature_flag = flag

        def has_permission(self, request, view) -> bool:
            if not (request.user and request.user.is_authenticated):
                return False
            plan = _plan(request.user)
            allowed = bool(getattr(plan, self.feature_flag, False)) if plan else False
            if not allowed:
                label = self.feature_flag.replace("_", " ").replace("can ", "").title()
                self.message = _upgrade_error(
                    label,
                    plan.tier if plan else "free",
                )
            return allowed

    _FeaturePermission.__name__ = f"Require_{flag}"
    return _FeaturePermission


# ── Limit-check mixin (for generics views) ────────────────────────────────────

class SubscriptionGateMixin:
    """
    Add to any generics view. Set class attributes to enable specific gates.

    Attributes:
        subscription_feature (str | None):
            Checks plan.<flag> == True before create/update.
            Example: subscription_feature = "can_access_bulk_upload"

        check_product_limit (bool):
            Before create, checks SubscriptionUsage.can_add_product() and
            increments the counter on success.

        check_image_limit (bool):
            Before create/update, checks uploaded image count against
            plan.max_images_per_product.
    """

    subscription_feature: str | None = None
    check_product_limit: bool = False
    check_image_limit: bool = False

    # ── Gates called by perform_create / perform_update ───────────────────────

    def _gate_feature(self) -> None:
        if not self.subscription_feature:
            return
        plan = _plan(self.request.user)
        if not plan or not getattr(plan, self.subscription_feature, False):
            label = self.subscription_feature.replace("_", " ").replace("can ", "").title()
            raise PermissionDenied(_upgrade_error(label, plan.tier if plan else "free"))

    def _gate_product_limit(self) -> None:
        usage = _usage(self.request.user)
        plan  = _plan(self.request.user)
        if usage and not usage.can_add_product():
            raise PermissionDenied({
                "error":        "product_limit_reached",
                "detail":       (
                    f"You've reached your plan limit of {plan.max_products} products. "
                    f"Upgrade your plan to add more."
                ),
                "limit":        plan.max_products if plan else 0,
                "current_tier": plan.tier if plan else "free",
                "action":       "upgrade",
                "upgrade_url":  "/subscribe",
            })

    def _gate_image_limit(self, image_count: int) -> None:
        plan = _plan(self.request.user)
        if not plan:
            return
        if image_count > plan.max_images_per_product:
            raise PermissionDenied({
                "error":        "image_limit_exceeded",
                "detail":       (
                    f"Your plan allows up to {plan.max_images_per_product} images per product "
                    f"({image_count} uploaded). Upgrade to add more."
                ),
                "limit":        plan.max_images_per_product,
                "current_tier": plan.tier,
                "action":       "upgrade",
                "upgrade_url":  "/subscribe",
            })

    def get_perform_create_kwargs(self) -> dict:
        """Override in subclasses to inject extra kwargs into serializer.save()."""
        return {}

    def perform_create(self, serializer):
        self._gate_feature()
        if self.check_product_limit:
            self._gate_product_limit()
        if self.check_image_limit:
            images = self.request.FILES.getlist("images[]", [])
            self._gate_image_limit(len(images))

        serializer.save(**self.get_perform_create_kwargs())

        if self.check_product_limit:
            usage = _usage(self.request.user)
            if usage:
                SubscriptionUsage.objects.filter(pk=usage.pk).update(
                    active_products_count=F("active_products_count") + 1
                )
    
    def perform_destroy(self, instance):
        super().perform_destroy(instance)
        if getattr(self, 'check_product_limit', False):
            usage = _usage(self.request.user)
            if usage and usage.active_products_count > 0:
                SubscriptionUsage.objects.filter(pk=usage.pk).update(
                    active_products_count=F("active_products_count") - 1
                )
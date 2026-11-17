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

def _vendor_of(user):
    # The store the user works for (owner or team member). Plan limits are
    # per store, so staff see exactly the features the owner paid for.
    return getattr(user, 'current_vendor', None)


def _active_sub(user) -> VendorSubscription | None:
    vendor = _vendor_of(user)
    if vendor is None:
        return None
    try:
        return (
            VendorSubscription.objects
            .select_related("plan")
            .get(vendor=vendor, status__in=["active", "trial"])
        )
    except VendorSubscription.DoesNotExist:
        return None


def _plan(user) -> SubscriptionPlan | None:
    # Same resolution everywhere (active/trial subscription, else free plan).
    from payments.entitlements import plan_for_vendor
    vendor = _vendor_of(user)
    if vendor is not None:
        return plan_for_vendor(vendor)
    return SubscriptionPlan.objects.filter(tier="free").order_by("price").first()


def _usage(user) -> SubscriptionUsage | None:
    vendor = _vendor_of(user)
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

    def _gate_product_limit(self, requested: int = 1) -> None:
        """
        Refuse when the store already has as many products as its plan
        allows. Counts the real products (a counter drifts). Call inside
        transaction.atomic() together with the create, so the store row lock
        stops two requests taking the last slot.
        """
        from payments.entitlements import ProductLimitReached, reserve_product_slots
        vendor = _vendor_of(self.request.user)
        if vendor is None:
            return
        try:
            reserve_product_slots(vendor, requested)
        except ProductLimitReached as exc:
            raise PermissionDenied(exc.payload(_plan(self.request.user)))

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
        from django.db import transaction
        from payments.entitlements import sync_usage
        self._gate_feature()
        if self.check_image_limit:
            images = self.request.FILES.getlist("images[]", [])
            self._gate_image_limit(len(images))

        # Limit check and create in one transaction (store row locked).
        with transaction.atomic():
            if self.check_product_limit:
                self._gate_product_limit()
            serializer.save(**self.get_perform_create_kwargs())

        if self.check_product_limit:
            vendor = _vendor_of(self.request.user)
            if vendor is not None:
                sync_usage(vendor)

    def perform_destroy(self, instance):
        super().perform_destroy(instance)
        if getattr(self, 'check_product_limit', False):
            from payments.entitlements import sync_usage
            vendor = _vendor_of(self.request.user)
            if vendor is not None:
                sync_usage(vendor)
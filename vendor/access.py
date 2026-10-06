"""
vendor/access.py
Single source of truth for two questions every seller endpoint asks:

  1. Which store does this user work for?   → get_membership / get_current_vendor
  2. What are they allowed to do there?     → has_capability / require_capability

Before team support, "is a vendor" meant User.role == 'vendor' and "my store"
meant Vendor.objects.get(user=request.user). Both are replaced by a VendorMember
lookup so that a store's owner, admins and staff all resolve to the same store,
each through their own personal Negromart account.

Performance: the membership (joined with its Vendor) is fetched once and
memoised on the user instance, so authentication + permission + view code in a
single request share one query.
"""

from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS

from .models import Vendor, VendorMember


# ── Capabilities ──────────────────────────────────────────────────────────────
# Views ask for a capability, never for a role, so adding a role later (e.g.
# "accountant") is a change to ROLE_CAPABILITIES only, not to every view.

class Capability:
    MANAGE_CATALOG = 'manage_catalog'   # products, bulk upload, review replies
    MANAGE_ORDERS = 'manage_orders'     # orders, shipments, tracking events
    VIEW_ANALYTICS = 'view_analytics'   # sales / traffic dashboards
    MANAGE_STORE = 'manage_store'       # store profile, hours, pause, prefs, activity log
    VIEW_FINANCE = 'view_finance'       # payouts, billing history, payout methods (read)
    MANAGE_FINANCE = 'manage_finance'   # payout methods, subscription, cards (write)
    MANAGE_TEAM = 'manage_team'         # invite / remove members
    CLOSE_STORE = 'close_store'         # account deletion request

    ALL = frozenset({
        MANAGE_CATALOG, MANAGE_ORDERS, VIEW_ANALYTICS, MANAGE_STORE,
        VIEW_FINANCE, MANAGE_FINANCE, MANAGE_TEAM, CLOSE_STORE,
    })


ROLE_CAPABILITIES = {
    VendorMember.ROLE_OWNER: Capability.ALL,
    VendorMember.ROLE_ADMIN: frozenset({
        Capability.MANAGE_CATALOG,
        Capability.MANAGE_ORDERS,
        Capability.VIEW_ANALYTICS,
        Capability.MANAGE_STORE,
        Capability.VIEW_FINANCE,
        Capability.MANAGE_TEAM,      # admins may invite/remove *staff* only (see team_views)
    }),
    VendorMember.ROLE_STAFF: frozenset({
        Capability.MANAGE_CATALOG,
        Capability.MANAGE_ORDERS,
        Capability.VIEW_ANALYTICS,
    }),
}


def capabilities_for_role(role):
    return ROLE_CAPABILITIES.get(role, frozenset())


# ── Membership lookup ─────────────────────────────────────────────────────────

_CACHE_ATTR = '_vendor_membership_cache'
_MISSING = object()


class NoVendorMembership(Vendor.DoesNotExist, AttributeError):
    """
    Raised by user.current_vendor when the user is in no store.

    Inherits from Vendor.DoesNotExist so existing `except Vendor.DoesNotExist`
    handlers keep working, and from AttributeError so
    `getattr(user, 'current_vendor', None)` returns None. Django's own
    RelatedObjectDoesNotExist (what `user.vendor_user` raised) does the same.
    """


def get_membership(user):
    """Return the user's active VendorMember (with .vendor loaded), or None."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    cached = user.__dict__.get(_CACHE_ATTR, _MISSING)
    if cached is not _MISSING:
        return cached
    membership = (
        VendorMember.objects
        .select_related('vendor')
        .filter(user_id=user.pk, is_active=True)
        .first()
    )
    user.__dict__[_CACHE_ATTR] = membership
    return membership


def clear_membership_cache(user):
    """Call after creating/removing a membership for a user object you still hold."""
    if user is not None:
        user.__dict__.pop(_CACHE_ATTR, None)


def get_current_vendor(user):
    """The user's store (any approval status). Raises NoVendorMembership if none."""
    membership = get_membership(user)
    if membership is None:
        raise NoVendorMembership("This account is not a member of any store.")
    return membership.vendor


def get_vendor_or_404(user):
    """Drop-in replacement for get_object_or_404(Vendor, user=request.user)."""
    membership = get_membership(user)
    if membership is None:
        raise Http404("No store is associated with this account.")
    return membership.vendor


def is_vendor(user):
    """
    May this user use the seller dashboard right now?

    Same meaning the old role == 'vendor' had (role flipped on approval and
    back on reject/suspend, both of which also toggle is_approved), but derived
    from live data, so it can never drift out of sync.
    """
    membership = get_membership(user)
    return bool(membership and membership.vendor.is_approved)


def has_capability(user, capability):
    membership = get_membership(user)
    if membership is None or not membership.vendor.is_approved:
        return False
    return capability in capabilities_for_role(membership.role)


def get_vendor_with_capability(user, capability):
    """
    Return the user's store if their role grants `capability`.

    - Not in any store       → None (callers keep their existing "no vendor" handling)
    - In a store, role lacks → raises PermissionDenied with a clear message
    """
    membership = get_membership(user)
    if membership is None:
        return None
    if capability not in capabilities_for_role(membership.role):
        raise PermissionDenied(_role_denied_message(membership.role))
    return membership.vendor


def get_finance_vendor(request):
    """Store for billing/payout endpoints: reads need VIEW_FINANCE, writes MANAGE_FINANCE."""
    capability = (
        Capability.VIEW_FINANCE if request.method in SAFE_METHODS
        else Capability.MANAGE_FINANCE
    )
    return get_vendor_with_capability(request.user, capability)


def _role_denied_message(role):
    label = dict(VendorMember.ROLE_CHOICES).get(role, role)
    return f"Your team role ({label}) does not allow this action. Ask the store owner for access."


# ── DRF permissions ───────────────────────────────────────────────────────────

class IsVendorMember(BasePermission):
    """Authenticated user with an active membership in an approved store."""
    message = "You must be a member of an approved store to use the seller dashboard."

    def has_permission(self, request, view):
        return is_vendor(request.user)


def require_capability(write, read=None):
    """
    Permission-class factory, used like the existing require_feature():

        permission_classes = [IsAuthenticated, IsVerifiedVendor,
                              require_capability(Capability.MANAGE_STORE)]

    `write` guards unsafe methods (POST/PUT/PATCH/DELETE). `read` guards
    GET/HEAD/OPTIONS; when omitted, `write` guards every method. Pass
    read=ANY_MEMBER to let every team member read.
    """
    read_capability = write if read is None else read

    class _RequireCapability(BasePermission):
        def has_permission(self, request, view):
            membership = get_membership(request.user)
            if membership is None or not membership.vendor.is_approved:
                self.message = IsVendorMember.message
                return False
            needed = read_capability if request.method in SAFE_METHODS else write
            if needed is ANY_MEMBER:
                return True
            if needed in capabilities_for_role(membership.role):
                return True
            self.message = _role_denied_message(membership.role)
            return False

    _RequireCapability.__name__ = f"RequireCapability_{write}"
    return _RequireCapability


# Sentinel for require_capability(read=ANY_MEMBER): every active member may read.
ANY_MEMBER = object()

"""
tools/access.py
Permission for the paid seller tools: the store's plan must include the tool
(tools/registry.py), and the team member needs the capability the tool acts on.
A locked tool answers 403 with the cheapest plan that unlocks it, so the
Seller Centre can show the right upgrade prompt.
"""
from rest_framework.permissions import BasePermission

from vendor.access import get_membership, require_capability

from . import registry


def require_tool(key):
    class _RequireTool(BasePermission):
        def has_permission(self, request, view):
            from payments.entitlements import plan_for_vendor
            membership = get_membership(request.user) if request.user and request.user.is_authenticated else None
            if membership is None:
                return False
            plan = plan_for_vendor(membership.vendor)
            if registry.included(plan, key):
                return True
            tool = registry.BY_KEY[key]
            upgrade = registry.cheapest_plans().get(key)
            self.message = {
                'error': 'plan_upgrade_required',
                'tool': key,
                'detail': f'{tool.name} is not included in your plan ({plan.name if plan else "Free"}).'
                          + (f' It comes with {upgrade}.' if upgrade else ''),
                'upgrade_plan': upgrade,
                'action': 'upgrade',
                'upgrade_url': '/subscribe',
            }
            return False

    _RequireTool.__name__ = f'RequireTool_{key}'
    return _RequireTool


def tool_permissions(key, capability, read=None):
    """Signed-in seller with the capability, on a plan that includes the tool."""
    from rest_framework.permissions import IsAuthenticated
    return [IsAuthenticated, require_capability(capability, read=read), require_tool(key)]

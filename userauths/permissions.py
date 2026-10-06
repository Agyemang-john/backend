# accounts/permissions.py

from rest_framework.permissions import BasePermission

class IsVendor(BasePermission):
    # Store access comes from team membership, not User.role (vendor/access.py).
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_active
            and request.user.is_vendor
        )

class IsManager(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_active
            and request.user.role == "manager"
        )

class RolePermission(BasePermission):
    """
    Generic role-based permission.
    Usage: permission_classes = [RolePermission]
    and set view.allowed_roles = ['vendor', 'manager']
    """
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_active
            and hasattr(view, "allowed_roles")
            and request.user.role in view.allowed_roles
        )

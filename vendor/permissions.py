from rest_framework import permissions
from rest_framework.permissions import BasePermission
from django.core.exceptions import PermissionDenied
from .models import Vendor
# Assuming is_vendor is defined in a utilities or helper module
from userauths.utils import is_vendor

class IsVendor(BasePermission):
    """
    Custom permission to check if a user is a vendor.
    """

    def has_permission(self, request, view):
        if request.user.is_authenticated:
            try:
                return is_vendor(request.user)
            except PermissionDenied:
                return False
        return False

class IsVerifiedVendor(BasePermission):
    """
    Allows access only to authenticated vendors who are verified, approved, non-suspended,
    and have an active subscription.
    """
    message = "Access denied: Vendor must be verified, approved, non-suspended, and have an active subscription."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            raise PermissionDenied("Authentication required.")
        if request.user.role != 'vendor':
            raise PermissionDenied("User must be a vendor.")
        try:
            vendor = Vendor.objects.get(user=request.user)
            if vendor.status != 'VERIFIED':
                raise PermissionDenied("Vendor is not verified.")
            if not vendor.is_approved:
                raise PermissionDenied("Vendor is not approved.")
            if vendor.is_suspended:
                raise PermissionDenied("Vendor is suspended.")
            # if not vendor.has_active_subscription():
            #     raise PermissionDenied("Vendor subscription is inactive or expired.")
            return True
        except Vendor.DoesNotExist:
            raise PermissionDenied("No vendor profile associated with this user.")

class IsAdminOrReadOnly(BasePermission):
    """
    Allows read-only access to all, but write access only to admins.
    """
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user and request.user.is_authenticated and request.user.is_staff

from rest_framework.permissions import BasePermission
from django.core.exceptions import PermissionDenied

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

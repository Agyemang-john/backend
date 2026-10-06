from django.contrib.auth.tokens import PasswordResetTokenGenerator
import six
import secrets
from django.utils import timezone
from datetime import timedelta


class AccountActivationTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return (
            six.text_type(user.pk) + six.text_type(timestamp) + six.text_type(user.is_active)
        )

account_activation_token = AccountActivationTokenGenerator()


class OTPTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return str(user.pk) + str(timestamp) + str(user.is_active)

    def generate_otp(self):
        return secrets.randbelow(90000) + 10000

otp_token_generator = OTPTokenGenerator()


from rest_framework_simplejwt.tokens import RefreshToken

class CustomVendorRefreshToken(RefreshToken):
    """
    Seller-dashboard token. Claims are display hints for the frontend (route
    guard, nav filtering); the API re-checks membership and capabilities on
    every request, so a stale claim can never grant access.
    """
    @classmethod
    def for_user(cls, user):
        from vendor.access import capabilities_for_role

        token = super().for_user(user)
        is_vendor = user.is_vendor
        # `role` stays 'vendor' for dashboard users so a seller proxy that is
        # still deployed with the old `payload.role === 'vendor'` check keeps
        # working during rollout. New code reads `is_vendor`.
        token["role"] = 'vendor' if is_vendor else user.role
        token["is_vendor"] = is_vendor
        token["is_staff"] = user.is_staff
        token["is_active"] = user.is_active
        token["token_version"] = user.token_version

        token["is_verified_vendor"] = False
        token["vendor_role"] = None
        token["vendor_capabilities"] = []
        membership = user.vendor_membership
        if is_vendor and membership is not None:
            vendor = membership.vendor
            token["is_verified_vendor"] = (
                vendor.status == 'VERIFIED' and
                vendor.is_approved and
                not vendor.is_suspended
            )
            token["vendor_role"] = membership.role
            token["vendor_capabilities"] = sorted(capabilities_for_role(membership.role))

        return token
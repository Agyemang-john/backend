from django.contrib.auth.tokens import PasswordResetTokenGenerator
import six
import secrets
from django.utils import timezone
from datetime import timedelta
from vendor.models import Vendor


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
    @classmethod
    def for_user(cls, user):
        token = super().for_user(user)
        token["role"] = user.role
        token["is_staff"] = user.is_staff
        token["is_active"] = user.is_active
        token["token_version"] = user.token_version

        # Add is_verified_vendor
        token["is_verified_vendor"] = False
        if user.role == 'vendor':
            try:
                vendor = user.vendor_user
                token["is_verified_vendor"] = (
                    vendor.status == 'VERIFIED' and
                    vendor.is_approved and
                    not vendor.is_suspended
                )
            except Vendor.DoesNotExist:
                pass

        return token
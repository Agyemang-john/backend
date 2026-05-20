from django.contrib.auth.tokens import PasswordResetTokenGenerator
import six  
import random
from django.utils.crypto import constant_time_compare
from django.utils import timezone
from datetime import timedelta
from vendor.models import Vendor


class AccountActivationTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return (
            six.text_type(user.pk) + six.text_type(timestamp)  + six.text_type(user.is_active)
        )

account_activation_token = AccountActivationTokenGenerator()


class OTPTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return str(user.pk) + str(timestamp) + str(user.is_active)

    def generate_otp(self):
        # Generate a random 5-digit OTP
        otp = random.randint(10000, 99999)
        return otp

    def check_token(self, user, otp, timestamp):
        try:
            otp_int = int(otp)
        except ValueError:
            return False
        return (
            constant_time_compare(self._make_hash_value(user, timestamp),
                                  self._make_hash_value(user,
                                                        self._num_minutes(
                                                            self.token_ttl) - timestamp))
            and constant_time_compare(self.generate_otp(), otp_int)
            and not self._is_token_expired(timestamp)
        )

    def _is_token_expired(self, timestamp):
        """Accepts a Unix float (time.time()). Returns True if past token_ttl."""
        import time as _time
        if isinstance(timestamp, (int, float)):
            return _time.time() > timestamp + self.token_ttl.total_seconds()
        # Fallback for legacy datetime values
        return timezone.now() > (timestamp + self.token_ttl)

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
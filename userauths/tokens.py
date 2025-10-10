from django.contrib.auth.tokens import PasswordResetTokenGenerator
import six  
import random
from django.utils.crypto import constant_time_compare
from django.utils.http import base36_to_int, int_to_base36
from django.utils import timezone
from datetime import timedelta


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
        """
        Check if the timestamp is older than TOKEN_TTL minutes.
        """
        expiration_time = self._num_minutes(self.token_ttl)
        return timezone.now() > (timestamp + timedelta(minutes=expiration_time))

    def _num_minutes(self, td):
        return td.days * 24 * 60 + td.seconds // 60 + td.microseconds / 60e6

otp_token_generator = OTPTokenGenerator()
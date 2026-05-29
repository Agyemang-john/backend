import secrets
import time
from datetime import timedelta
from django.core.cache import cache
from django.utils.http import urlsafe_base64_encode
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes

class OTPTokenGenerator:
    token_ttl = timedelta(minutes=10)

    def generate_otp(self):
        return secrets.randbelow(90000) + 10000

    def _is_token_expired(self, timestamp):
        """
        timestamp must be a Unix float (time.time()).
        Returns True if the OTP is older than token_ttl.
        """
        ttl_seconds = self.token_ttl.total_seconds()
        return time.time() > (timestamp + ttl_seconds)

otp_token_generator = OTPTokenGenerator()

def cache_activation_data(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    email_token = default_token_generator.make_token(user)

    cache_key = f"activation:{user.id}"

    cache_data = {
        "uid": uid,
        "email_token": email_token,
    }

    # Optional: store for debugging or fallback (not required)
    cache.set(cache_key, cache_data, timeout=15 * 60)

    return cache_data

import random
from datetime import timedelta
from django.core.cache import cache
from django.utils import timezone
from django.utils.http import urlsafe_base64_encode
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes

class OTPTokenGenerator:
    token_ttl = timedelta(minutes=10)

    def generate_otp(self):
        return random.randint(10000, 99999)
    
    def _is_token_expired(self, timestamp):
        expiration_time = self._num_minutes(self.token_ttl)
        return timezone.now() > (timestamp + timedelta(minutes=expiration_time))

    def _num_minutes(self, td):
        return td.days * 24 * 60 + td.seconds // 60 + td.microseconds / 60e6

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

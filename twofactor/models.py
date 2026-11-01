from django.conf import settings
from django.db import models


class TOTPDevice(models.Model):
    """
    One authenticator app per account. The shared secret is stored encrypted
    (FERNET_KEY); recovery codes are stored hashed and each works once.
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='totp_device')
    secret_encrypted = models.TextField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # Highest time-step accepted so far: a code can't be replayed.
    last_step = models.BigIntegerField(default=0)
    recovery_hashes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def confirmed(self):
        return self.confirmed_at is not None

    def __str__(self):
        return f"Authenticator for user {self.user_id}"

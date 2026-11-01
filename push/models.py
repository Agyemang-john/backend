from django.conf import settings
from django.db import models


class PushSubscription(models.Model):
    """One device/browser that accepted notifications, for one app."""
    SHOP, SELLER = 'shop', 'seller'
    APP_CHOICES = [(SHOP, 'Shop'), (SELLER, 'Seller dashboard')]
    WEBPUSH, FCM = 'webpush', 'fcm'
    KIND_CHOICES = [(WEBPUSH, 'Web push'), (FCM, 'Firebase (mobile app)')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='push_subscriptions')
    app = models.CharField(max_length=6, choices=APP_CHOICES)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=WEBPUSH)
    endpoint = models.TextField(help_text="Web push endpoint URL, or the FCM registration token.")
    endpoint_hash = models.CharField(max_length=64, unique=True, editable=False)
    p256dh = models.CharField(max_length=200, blank=True, default='')
    auth = models.CharField(max_length=100, blank=True, default='')
    user_agent = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    failures = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=['user', 'app'], name='push_user_app_idx')]

    def save(self, *args, **kwargs):
        import hashlib
        self.endpoint_hash = hashlib.sha256(self.endpoint.encode()).hexdigest()
        super().save(*args, **kwargs)

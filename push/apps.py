from django.apps import AppConfig


class PushConfig(AppConfig):
    """Web push (VAPID) for the shop and seller apps, FCM for the mobile app."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'push'

    def ready(self):
        from . import signals  # noqa: F401

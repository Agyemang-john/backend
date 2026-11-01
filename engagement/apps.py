from django.apps import AppConfig


class EngagementConfig(AppConfig):
    """
    Shopper re-engagement: price-drop / back-in-stock alerts, abandoned-cart
    reminders and the per-category email opt-outs they share.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'engagement'

    def ready(self):
        from . import signals  # noqa: F401

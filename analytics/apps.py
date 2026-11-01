from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    """First-party funnel analytics and A/B experiments."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'analytics'

    def ready(self):
        from . import signals  # noqa: F401

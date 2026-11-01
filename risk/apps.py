from django.apps import AppConfig


class RiskConfig(AppConfig):
    """Order fraud scoring: rules → score → hold for review; COD abuse gate."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'risk'
    verbose_name = 'Order risk'

    def ready(self):
        from . import signals  # noqa: F401

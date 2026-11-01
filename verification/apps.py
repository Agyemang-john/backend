from django.apps import AppConfig


class VerificationConfig(AppConfig):
    """Seller KYC review: ID numbers, per-document decisions, resubmission, audit trail."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'verification'
    verbose_name = 'Seller verification (KYC)'

    def ready(self):
        from . import signals  # noqa: F401

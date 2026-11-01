from django.apps import AppConfig


class ProtectionConfig(AppConfig):
    """Negromart Guarantee: buyer claims against sellers, with payout holds and staff decisions."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'protection'
    verbose_name = 'Buyer protection (claims)'

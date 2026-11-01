from django.apps import AppConfig


class AdsConfig(AppConfig):
    """Sponsored products: seller campaigns, auction, click billing to the seller ledger."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ads'
    verbose_name = 'Sponsored products'

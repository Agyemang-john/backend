from django.apps import AppConfig


class CatalogConfig(AppConfig):
    """Multi-seller catalog: grouping listings of the same item and picking the featured offer."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'catalog'

    def ready(self):
        from . import signals  # noqa: F401

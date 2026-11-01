from django.apps import AppConfig


class PickupConfig(AppConfig):
    """Pickup points (click & collect): stations, parcel codes, collection."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pickup'

    def ready(self):
        from . import signals  # noqa: F401

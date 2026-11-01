from django.apps import AppConfig


class TwofactorConfig(AppConfig):
    """Authenticator-app (TOTP) two-factor sign-in: required for staff, optional for sellers."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "twofactor"
    verbose_name = "Two-factor authentication"

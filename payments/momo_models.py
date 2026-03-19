# subscriptions/momo_models.py
# Add these models to your subscriptions/models.py (or import from here)
#
# Run after adding:
#   python manage.py makemigrations subscriptions
#   python manage.py migrate

from django.db import models
from django.core.validators import RegexValidator
from vendor.models import Vendor


# ─────────────────────────────────────────────────────────────────────────────
# MoMo Account — saved mobile money numbers for auto-billing
# ─────────────────────────────────────────────────────────────────────────────

class MomoAccount(models.Model):
    """
    Stores a vendor's saved Mobile Money accounts.
    Paystack supports recurring mobile money charges in Ghana via the
    /charge endpoint with mobile_money channel. Each charge triggers
    a USSD/OTP prompt — no token stored (unlike card authorizations).
    We store the number + provider so we can re-charge automatically.
    """
    PROVIDER_CHOICES = [
        ('mtn',       'MTN Mobile Money'),
        ('vodafone',  'Vodafone Cash'),
        ('airteltigo','AirtelTigo Money'),
    ]

    vendor    = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='momo_accounts')
    provider  = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    phone     = models.CharField(
        max_length=20,
        validators=[RegexValidator(r'^\+?[0-9]{10,15}$', 'Enter a valid phone number')],
        help_text="Include country code e.g. +233XXXXXXXXX"
    )
    nickname  = models.CharField(max_length=60, blank=True, help_text="e.g. 'My MTN number'")
    is_default = models.BooleanField(default=False)

    # Paystack charges mobile money on demand — no stored token needed.
    # We just store the last Paystack reference for audit purposes.
    last_reference = models.CharField(max_length=200, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "MoMo Account"
        verbose_name_plural = "MoMo Accounts"
        unique_together = [('vendor', 'phone')]  # one entry per number per vendor
        ordering = ['-is_default', '-created_at']

    def __str__(self):
        return f"{self.get_provider_display()} {self.phone} ({self.vendor.name})"

    @property
    def masked_phone(self):
        """Returns e.g. +233 XX XXX XX89 for display."""
        p = self.phone.replace(' ', '')
        if len(p) >= 4:
            return p[:-4].replace(p[:-4], '*' * len(p[:-4])) + p[-4:]
        return p

    @property
    def display_name(self):
        label = self.nickname or self.get_provider_display()
        return f"{label} — ••••{self.phone[-4:]}"


# ─────────────────────────────────────────────────────────────────────────────
# Billing Profile — vendor's billing/invoice details
# ─────────────────────────────────────────────────────────────────────────────

class BillingProfile(models.Model):
    """
    Billing/invoice details for a vendor.
    Required before initiating any subscription payment.
    Used to pre-fill checkout forms and generate invoice PDFs.
    """
    vendor       = models.OneToOneField(Vendor, on_delete=models.CASCADE, related_name='billing_profile')

    # Personal / business identity
    first_name     = models.CharField(max_length=100)
    last_name      = models.CharField(max_length=100)
    email          = models.EmailField(help_text="Billing email — may differ from login email")
    phone          = models.CharField(
        max_length=20,
        blank=True,
        validators=[RegexValidator(r'^\+?[0-9]{10,15}$', 'Enter a valid phone number')],
    )
    business_name  = models.CharField(max_length=200, blank=True)

    # Address
    address_line1  = models.CharField(max_length=200, blank=True)
    address_line2  = models.CharField(max_length=200, blank=True)
    city           = models.CharField(max_length=100, blank=True)
    region         = models.CharField(max_length=100, blank=True, help_text="State / region / province")
    postal_code    = models.CharField(max_length=20, blank=True)
    country        = models.CharField(max_length=2, default='GH', help_text="ISO 3166-1 alpha-2")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Billing Profile"
        verbose_name_plural = "Billing Profiles"

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.vendor.name})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_complete(self):
        """
        Returns True when the minimum required fields are filled.
        A billing profile must be complete before a payment can proceed.
        """
        return bool(self.first_name and self.last_name and self.email)
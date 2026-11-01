"""
Sales tax (VAT and levies) for invoices and receipts.

Storefront prices are tax-inclusive, so tax never changes what a shopper pays:
it is the part of each price that a VAT-registered seller owes the tax
authority, shown on invoices. Sellers that are not VAT-registered charge none.

Rates are per supplier country and can have several components (Ghana: VAT,
NHIL, GETFund). Each component is a % of the tax-exclusive value, so the tax
inside an inclusive price P is P * R / (100 + R) for the combined rate R.
Categories can be exempt per country (e.g. unprocessed food, books).
"""
from django.db import models


class TaxRate(models.Model):
    country = models.CharField(max_length=2, help_text="ISO country code of the seller, e.g. GH.")
    code = models.SlugField(max_length=20, help_text="Short code, e.g. vat, nhil, getfund.")
    name = models.CharField(max_length=60, help_text="Shown on invoices, e.g. 'VAT (15%)'.")
    rate = models.DecimalField(max_digits=5, decimal_places=2, help_text="Percent of the tax-exclusive value.")
    position = models.PositiveSmallIntegerField(default=100)
    active = models.BooleanField(default=True)
    effective_from = models.DateField(null=True, blank=True,
                                      help_text="Orders before this date use the previous rate. Blank = always.")

    class Meta:
        ordering = ('country', 'position', 'code')
        indexes = [models.Index(fields=['country', 'active'], name='tax_rate_country_idx')]

    def __str__(self):
        return f"{self.country} {self.name} {self.rate}%"


class TaxExemption(models.Model):
    """Goods in this category carry no VAT in this country."""
    country = models.CharField(max_length=2)
    category = models.ForeignKey('product.Category', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    sub_category = models.ForeignKey('product.Sub_Category', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    reason = models.CharField(max_length=120, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(category__isnull=False) | models.Q(sub_category__isnull=False),
                name='tax_exemption_has_target'),
        ]

    def __str__(self):
        return f"{self.country} exempt: {self.sub_category or self.category}"


class SellerTaxRegistration(models.Model):
    """A store's VAT status. No row = not registered (charges no VAT)."""
    vendor = models.OneToOneField('vendor.Vendor', on_delete=models.CASCADE, related_name='tax_registration')
    vat_registered = models.BooleanField(default=False)
    tax_id = models.CharField(max_length=30, blank=True, help_text="TIN / VAT number printed on invoices.")
    registered_name = models.CharField(max_length=128, blank=True, help_text="Legal name if different from the store name.")
    verified = models.BooleanField(default=False, help_text="Staff checked the certificate.")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.vendor} VAT={'yes' if self.vat_registered else 'no'}"

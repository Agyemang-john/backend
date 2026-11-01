"""
Order receipts (for the shopper, whole order) and seller invoices (one per
store in the order). Each is numbered once from a database sequence and
stores a frozen snapshot of what was bought, so later product edits, price
changes or seller renames never alter an issued document. PDFs are rendered
from the snapshot on request (invoices/pdf.py); nothing is stored as a file.
"""
from django.db import models


class Invoice(models.Model):
    RECEIPT = 'receipt'     # to the shopper, every line of the order
    SELLER = 'seller'       # one store's lines, for the store's books / packing
    KIND_CHOICES = [(RECEIPT, 'Order receipt'), (SELLER, 'Seller invoice')]

    number = models.CharField(max_length=24, unique=True)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    order = models.ForeignKey('order.Order', on_delete=models.PROTECT, related_name='invoices')
    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    issued_at = models.DateTimeField(auto_now_add=True)
    currency = models.CharField(max_length=3, default='GHS')
    total = models.DecimalField(max_digits=12, decimal_places=2)
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    data = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['order', 'kind', 'vendor'], nulls_distinct=False, name='uniq_invoice_per_order'),
        ]
        indexes = [models.Index(fields=['vendor', '-issued_at'], name='invoice_vendor_idx')]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number}"

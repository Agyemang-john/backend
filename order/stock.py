"""
Stock deduction at order time.

One conditional UPDATE per line: it only succeeds while enough stock is left,
so two checkouts can't both take the last unit, and it doesn't re-validate
unrelated product fields (product.full_clean() used to fail checkout for any
product whose brand had been deleted).
"""
from django.core.exceptions import ValidationError
from django.db.models import F

from product.models import Product, Variants


def deduct_stock(product, variant, quantity):
    """Take `quantity` units off the variant (or the product). Raises ValidationError if short."""
    if variant is not None:
        updated = Variants.objects.filter(pk=variant.pk, quantity__gte=quantity).update(
            quantity=F('quantity') - quantity
        )
        label = f"{product.title} ({variant.title})"
    else:
        updated = Product.objects.filter(pk=product.pk, total_quantity__gte=quantity).update(
            total_quantity=F('total_quantity') - quantity
        )
        label = product.title
    if not updated:
        raise ValidationError(f"Not enough stock for {label}.")

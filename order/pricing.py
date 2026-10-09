"""
Flash-sale aware pricing for cart lines and order lines.

The flash price is never stored on the cart: it's looked up live, so it
ends when the sale ends, and a capped sale (max_quantity) only discounts
the units it has left — the rest of the line is charged at the normal price.
Order creation reserves those units (sold_count) under a row lock.
"""
from decimal import Decimal

from django.db.models import F

from product.models import FlashSale


def base_price(product, variant=None):
    return variant.price if variant else product.price


def line_pricing(product, variant, quantity, sale=None, use_live_sale=True):
    """
    Price `quantity` units. Returns a dict:
      unit_price   price per unit shown to the customer (flash price if any unit gets it)
      amount       line total
      flash_sale   the FlashSale applied, or None
      flash_units  how many units get the flash price
    """
    base = base_price(product, variant)
    if sale is None and use_live_sale:
        sale = FlashSale.live_for(product, variant)
    if sale is None or quantity <= 0:
        return {'unit_price': base, 'amount': base * quantity, 'flash_sale': None, 'flash_units': 0}

    remaining = sale.stock_remaining  # None = uncapped
    flash_units = quantity if remaining is None else min(quantity, remaining)
    if flash_units <= 0:
        return {'unit_price': base, 'amount': base * quantity, 'flash_sale': None, 'flash_units': 0}

    amount = sale.sale_price * flash_units + base * (quantity - flash_units)
    return {
        'unit_price': sale.sale_price,
        'amount': amount,
        'flash_sale': sale,
        'flash_units': flash_units,
    }


def order_unit_price(amount, quantity):
    """OrderProduct.price: the average when only part of a line got the flash price."""
    return (Decimal(amount) / quantity).quantize(Decimal('0.01')) if quantity else Decimal(amount)


def reserve_flash_units(flash_sale_id, units):
    """
    Count `units` against a sale's max_quantity. Call inside transaction.atomic().
    Locks the row so two checkouts can't both take the last units; returns how
    many were actually reserved (fewer if the cap was reached meanwhile).
    """
    if not flash_sale_id or units <= 0:
        return 0
    sale = FlashSale.objects.select_for_update().filter(id=flash_sale_id).first()
    if sale is None:
        return 0
    if sale.max_quantity is not None:
        units = min(units, max(sale.max_quantity - sale.sold_count, 0))
    if units:
        FlashSale.objects.filter(id=sale.id).update(sold_count=F('sold_count') + units)
    return units


def price_and_reserve(product, variant, quantity):
    """
    Price an order line from the live sale and reserve its flash units in one go.
    Call inside transaction.atomic(). Returns (unit_price, amount).
    """
    sale = FlashSale.live_for(product, variant)
    if sale is not None:
        # Re-read under lock so the cap check sees concurrent checkouts.
        sale = FlashSale.objects.select_for_update().get(id=sale.id)
    pricing = line_pricing(product, variant, quantity, sale=sale, use_live_sale=False)
    if pricing['flash_units']:
        reserve_flash_units(sale.id, pricing['flash_units'])
    return order_unit_price(pricing['amount'], quantity), pricing['amount']

"""Grouping listings into CatalogItems, and letting a store offer an item that's already listed."""
import logging

from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)


class CatalogError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ── GTIN ─────────────────────────────────────────────────────────────────────

def clean_gtin(raw):
    """Digits-only GTIN-8/12/13/14 with a valid check digit, zero-padded to 14; None if invalid."""
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if len(digits) not in (8, 12, 13, 14):
        return None
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    if (10 - total % 10) % 10 != check:
        return None
    return digits.zfill(14)


def attach_by_gtin(product):
    """Put a product with a GTIN into that GTIN's CatalogItem (creating it). Returns the item id or None."""
    from product.models import CatalogItem, Product
    if not product.gtin:
        return None
    item = CatalogItem.objects.filter(gtin=product.gtin).first()
    if item is None:
        try:
            with transaction.atomic():
                item = CatalogItem.objects.create(title=product.title, gtin=product.gtin, brand_id=product.brand_id,
                                                  sub_category_id=product.sub_category_id)
        except IntegrityError:   # created concurrently
            item = CatalogItem.objects.get(gtin=product.gtin)
    if product.catalog_item_id != item.pk:
        old = product.catalog_item_id
        Product.objects.filter(pk=product.pk).update(catalog_item=item)
        product.catalog_item_id = item.pk
        _after_regroup({item.pk, old} - {None})
    return item.pk


def group(products, title=None):
    """Staff: these listings are the same item. Returns the CatalogItem."""
    from product.models import CatalogItem, Product
    products = list(products)
    if len(products) < 2:
        raise CatalogError("Pick at least two listings.")
    if len({p.vendor_id for p in products}) < len(products):
        raise CatalogError("Two of these are from the same store; a store has one listing per item.")
    existing = {p.catalog_item_id for p in products} - {None}
    gtins = {p.gtin for p in products if p.gtin}
    if len(gtins) > 1:
        raise CatalogError("These have different barcodes, so they aren't the same item.")
    with transaction.atomic():
        if existing:
            item = CatalogItem.objects.filter(pk__in=existing).order_by('pk').first()
        else:
            first = products[0]
            item = CatalogItem.objects.create(title=title or first.title, gtin=next(iter(gtins), ''),
                                              brand_id=first.brand_id, sub_category_id=first.sub_category_id)
        Product.objects.filter(pk__in=[p.pk for p in products]).update(catalog_item=item)
        # Merge any other groups these listings belonged to.
        Product.objects.filter(catalog_item_id__in=existing - {item.pk}).update(catalog_item=item)
        CatalogItem.objects.filter(pk__in=existing - {item.pk}).delete()
    _after_regroup({item.pk})
    return item


def ungroup(product):
    from product.models import Product
    old = product.catalog_item_id
    Product.objects.filter(pk=product.pk).update(catalog_item=None, is_buybox_winner=True)
    _after_regroup({old} - {None})


def _after_regroup(item_ids):
    from .buybox import recompute
    if item_ids:
        transaction.on_commit(lambda: recompute(item_ids))


# ── "Sell one like this" ─────────────────────────────────────────────────────

COPIED_FIELDS = ('title', 'sub_category_id', 'brand_id', 'image', 'video', 'features', 'description',
                 'specifications', 'weight', 'volume', 'warranty_period_days', 'gtin')


@transaction.atomic
def create_offer(vendor, source, *, price, quantity, condition='new', seller_sku='', return_period_days=None):
    """
    A store lists an item that's already on the site. The new listing reuses the
    item's details and photos (not copied files; product photos aren't deleted
    with products) and takes this store's price, stock and condition. Items
    sold in sizes/colours need their own listing with the same barcode.
    """
    from attributes.models import ProductAttributeValue
    from product.models import CatalogItem, Product, ProductDeliveryOption, ProductImages

    if source.status != 'published':
        raise CatalogError("This item isn't available to sell.", 404)
    if source.vendor_id == vendor.pk:
        raise CatalogError("This is already your listing.")
    if source.variant != 'None':
        raise CatalogError("Items with sizes or colours need their own listing. Create it with the same "
                           "barcode and it will be grouped with this one.")
    item_id = source.catalog_item_id
    if item_id and Product.objects.filter(catalog_item_id=item_id, vendor=vendor).exists():
        raise CatalogError("You already sell this item.", 409)
    if Product.objects.filter(vendor=vendor, title__iexact=source.title).exists():
        raise CatalogError("You already have a product with this title.", 409)

    if item_id is None:
        item = CatalogItem.objects.create(title=source.title, gtin=source.gtin, brand_id=source.brand_id,
                                          sub_category_id=source.sub_category_id)
        Product.objects.filter(pk=source.pk).update(catalog_item=item)
        item_id = item.pk

    offer = Product(vendor=vendor, catalog_item_id=item_id, variant='None', price=price, old_price=price,
                    total_quantity=quantity, product_type=condition, seller_sku=seller_sku or '',
                    return_period_days=source.return_period_days if return_period_days is None else return_period_days,
                    delivery_returns=source.delivery_returns,
                    # The listing content was already reviewed; the store only adds an offer.
                    status='published', is_buybox_winner=False)
    for f in COPIED_FIELDS:
        setattr(offer, f, getattr(source, f))
    offer.save()
    offer.available_in_regions.set(source.available_in_regions.all())
    ProductImages.objects.bulk_create([ProductImages(product=offer, images=img.images)
                                       for img in source.p_images.all()])
    ProductDeliveryOption.objects.bulk_create([
        ProductDeliveryOption(product=offer, delivery_option_id=d.delivery_option_id, default=d.default)
        for d in ProductDeliveryOption.objects.filter(product=source, variant__isnull=True)])
    ProductAttributeValue.objects.bulk_create([
        ProductAttributeValue(product=offer, definition_id=v.definition_id, option_id=v.option_id,
                              value_number=v.value_number, value_bool=v.value_bool, value_text=v.value_text)
        for v in ProductAttributeValue.objects.filter(product=source)])
    _after_regroup({item_id})
    return offer

"""
vendor/bulk_upload.py
Many products at once, from a spreadsheet (CSV) or the in-browser table.

One row = one product, or one version of a product (Shopify-style):
rows with the same title are one product, and each row's option columns
(option1_name / option1_value ... option3) describe that version, with its
own price, stock, SKU and photo. Up to 3 options per product, any of the
types in product/variations.py or a name of the seller's own.

    title,                 category, price, stock, weight_kg, option1_name, option1_value, option2_name, option2_value
    Protein Shake,         sports-nutrition, 120, 10, 0.6,    Flavour,      Vanilla,       Size,         500 g
    Protein Shake,         ,                 200,  4, 1.1,    Flavour,      Vanilla,       Size,         1 kg

Everything a product needs to be sold is filled in: ships-to countries and
delivery methods default to the store's own (copied from its latest product),
photos come from links (downloaded and checked, vendor/remote_images.py) or
from files attached in the table. A product without a photo is saved as a
draft for the seller to finish; one with a photo goes to review.

COLUMNS is the single description of the format: the template, the column
list on the upload page and the seller guide all read it.
"""
import html
import logging
import re
import uuid
from decimal import Decimal, InvalidOperation

from django.core.files.base import ContentFile
from django.db import transaction

from product import variations
from product.models import (
    Brand, DeliveryOption, Product, ProductDeliveryOption, ProductImages, Sub_Category, Variants,
)
from vendor.remote_images import RemoteImageError, fetch_photo, to_product_photo

logger = logging.getLogger(__name__)

MAX_ROWS = 500
MAX_OPTIONS = variations.MAX_AXES

# name, required, what to put in it, example
COLUMNS = [
    ('title', True, 'Product name shoppers see. Rows with the same title are versions of one product.', 'Protein Shake'),
    ('category', True, 'Category code from the list on the upload page (or the category name).', 'sports-nutrition'),
    ('price', True, 'Selling price in GHS. For versions: the price of this version.', '120.00'),
    ('stock', True, 'How many you have. For versions: stock of this version.', '25'),
    ('weight_kg', True, 'Packed weight in kg, used for delivery prices.', '0.6'),
    ('compare_at_price', False, 'Old price shown crossed out. Leave empty if there is no discount.', '150.00'),
    ('condition', False, 'new, used, refurbished, grocery or book. Empty = new.', 'grocery'),
    ('brand', False, 'Brand from the list on the upload page. Leave empty if none.', ''),
    ('description', False, 'What shoppers read. Plain text; new lines become paragraphs.', 'Whey protein with 24 g per scoop.'),
    ('features', False, 'Key features or ingredients, one per line or separated by ;', 'Gluten free; 24 g protein'),
    ('specifications', False, 'Extra details (size, materials...).', ''),
    ('delivery_returns', False, 'Your delivery and returns note.', ''),
    ('return_days', False, 'Days the buyer has to return it. Empty = no returns.', '7'),
    ('seller_sku', False, 'Your own code for this product or version. Must be unique in your store.', 'PS-VAN-500'),
    ('barcode', False, 'GTIN/EAN/UPC barcode (8, 12, 13 or 14 digits).', ''),
    ('image_url', False, 'Link to the main photo (or this version\'s photo). Direct image, Google Drive or Dropbox link.', 'https://example.com/shake-vanilla.jpg'),
    ('more_image_urls', False, 'More photo links, separated by ;', ''),
    ('ships_to', False, 'Countries you ship to (codes or names, separated by ;). Empty = the same as your latest product.', 'GH'),
    ('delivery_methods', False, 'Delivery methods by name, separated by ;. The first is the default. Empty = the same as your latest product.', ''),
    ('option1_name', False, 'What changes between versions, e.g. Colour, Size, Flavour, Scent, Pack size.', 'Flavour'),
    ('option1_value', False, 'This version\'s value for option 1.', 'Vanilla'),
    ('option2_name', False, 'Second option (optional).', 'Size'),
    ('option2_value', False, 'This version\'s value for option 2.', '500 g'),
    ('option3_name', False, 'Third option (optional).', ''),
    ('option3_value', False, 'This version\'s value for option 3.', ''),
]
TEMPLATE_HEADERS = [c[0] for c in COLUMNS]
REQUIRED_COLUMNS = [c[0] for c in COLUMNS if c[1]]

# Older column names still accepted (the first template and its users).
ALIASES = {
    'sub_category_slug': 'category', 'sub_category': 'category', 'brand_slug': 'brand',
    'old_price': 'compare_at_price', 'total_quantity': 'stock', 'quantity': 'stock',
    'product_type': 'condition', 'weight': 'weight_kg', 'gtin': 'barcode',
    'return_period_days': 'return_days', 'sku': 'seller_sku', 'image': 'image_url',
}

CONDITIONS = {'new': 'new', 'used': 'used', 'second-hand': 'used', 'refurbished': 'refurbished',
              'grocery': 'grocery', 'food': 'grocery', 'book': 'book'}

# Rows of the template's examples start with this and are skipped.
EXAMPLE_MARK = '#'


class SpecError(Exception):
    """errors: {field: message} safe to show to the seller."""

    def __init__(self, errors):
        super().__init__(str(errors))
        self.errors = errors


def template_rows():
    """Example rows for the downloadable template: skipped if left in the file."""
    blank = dict.fromkeys(TEMPLATE_HEADERS, '')
    simple = {**blank, 'title': '# Example: Cotton Tote Bag (delete these example rows)', 'category': 'bags',
              'price': '45.00', 'stock': '30', 'weight_kg': '0.3', 'compare_at_price': '60.00', 'condition': 'new',
              'description': 'Strong cotton tote with inner pocket.', 'image_url': 'https://example.com/tote.jpg',
              'return_days': '7'}
    versions = [
        {**blank, 'title': '# Example: Protein Shake', 'category': 'sports-nutrition', 'price': '120.00',
         'stock': '25', 'weight_kg': '0.6', 'condition': 'grocery', 'description': 'Whey protein, 24 g per scoop.',
         'image_url': 'https://example.com/shake-vanilla-500.jpg', 'seller_sku': 'PS-VAN-500',
         'option1_name': 'Flavour', 'option1_value': 'Vanilla', 'option2_name': 'Size', 'option2_value': '500 g'},
        {**blank, 'title': '# Example: Protein Shake', 'price': '200.00', 'stock': '10', 'weight_kg': '1.1',
         'seller_sku': 'PS-VAN-1KG', 'option1_name': 'Flavour', 'option1_value': 'Vanilla',
         'option2_name': 'Size', 'option2_value': '1 kg'},
        {**blank, 'title': '# Example: Protein Shake', 'price': '120.00', 'stock': '0', 'weight_kg': '0.6',
         'image_url': 'https://example.com/shake-choc-500.jpg', 'seller_sku': 'PS-CHO-500',
         'option1_name': 'Flavour', 'option1_value': 'Chocolate', 'option2_name': 'Size', 'option2_value': '500 g'},
    ]
    return [simple, *versions]


# ── Reading rows ─────────────────────────────────────────────────────────────

def normalise_row(raw):
    """Lower-case, trimmed column names, older names mapped to the current ones."""
    row = {}
    for key, value in (raw or {}).items():
        if key is None:
            continue
        name = str(key).strip().lower().replace(' ', '_')
        name = ALIASES.get(name, name)
        value = '' if value is None else str(value).strip()
        if value or name not in row:      # an old and a new column for the same thing: the filled one wins
            row[name] = value
    return row


def _split(value):
    return [v.strip() for v in re.split(r'[;\n]', value or '') if v.strip()]


def _legacy_versions(row):
    """The first template's variant/size_names/color_names columns → option columns, one row per version."""
    kind = row.get('variant', '')
    if kind not in ('Size', 'Color', 'Size-Color'):
        return [row]
    sizes = _split(row.get('size_names', '').replace(',', ';'))
    colors = _split(row.get('color_names', '').replace(',', ';'))
    prices = _split(row.get('variant_prices', ''))
    stocks = _split(row.get('variant_quantities', ''))
    combos = ([{'Size': s} for s in sizes] if kind == 'Size' else
              [{'Colour': c} for c in colors] if kind == 'Color' else
              [{'Size': s, 'Colour': c} for s in sizes for c in colors])
    out = []
    for i, combo in enumerate(combos):
        version = dict(row)
        for n, (name, value) in enumerate(combo.items(), start=1):
            version[f'option{n}_name'], version[f'option{n}_value'] = name, value
        version['price'] = prices[i] if i < len(prices) else row.get('price', '')
        version['stock'] = stocks[i] if i < len(stocks) else row.get('stock', '')
        out.append(version)
    return out or [row]


def rows_to_specs(raw_rows, first_row_number=2):
    """
    Spreadsheet rows → one spec per product: {'rows': [row numbers], 'product': first row,
    'versions': [rows]}. Example rows (title starting with #) and empty rows are skipped.
    """
    groups, order = {}, []
    for number, raw in enumerate(raw_rows, start=first_row_number):
        row = normalise_row(raw)
        if not any(row.values()):
            continue
        title = ' '.join(row.get('title', '').split())
        if title.startswith(EXAMPLE_MARK):
            continue
        key = title.casefold() or f'__row{number}'
        expanded = _legacy_versions(row)
        if key not in groups:
            groups[key] = {'rows': [], 'product': expanded[0], 'versions': []}
            order.append(key)
        group = groups[key]
        group['rows'].append(number)
        group['versions'].extend(expanded)
    return [groups[k] for k in order]


# ── Lookups shared by every row of one upload ────────────────────────────────

class Context:
    """Categories, brands, countries and the store's defaults, loaded once per upload."""

    def __init__(self, vendor):
        from address.models import Country
        from payments.entitlements import plan_for_vendor

        self.vendor = vendor
        self.categories = {}
        for sc in Sub_Category.objects.select_related('category__main_category'):
            for name in (sc.slug, sc.title):
                if name:
                    self.categories.setdefault(name.casefold(), sc)
        self.brands = {}
        for b in Brand.objects.all():
            for name in (b.slug, b.title):
                if name:
                    self.brands.setdefault(name.casefold(), b)
        self.countries = {}
        for c in Country.objects.all():
            for name in (c.code, c.name):
                if name:
                    self.countries.setdefault(name.casefold(), c)
        self.delivery = {d.name.casefold(): d for d in DeliveryOption.objects.all()}

        latest = (Product.objects.filter(vendor=vendor).order_by('-updated')
                  .prefetch_related('available_in_regions', 'productdeliveryoption_set__delivery_option').first())
        self.default_regions = list(latest.available_in_regions.all()) if latest else []
        if not self.default_regions:
            home = vendor.shipping_from_country or Country.objects.filter(code__iexact='GH').first()
            self.default_regions = [home] if home else []
        self.default_delivery = []
        if latest:
            for pdo in latest.productdeliveryoption_set.filter(variant__isnull=True).order_by('-default', 'id'):
                self.default_delivery.append(pdo.delivery_option)
        if not self.default_delivery:
            local = list(DeliveryOption.objects.filter(type=DeliveryOption.LOCAL).order_by('cost', 'id')[:1])
            self.default_delivery = local or list(DeliveryOption.objects.order_by('id')[:1])

        plan = plan_for_vendor(vendor)
        self.max_images = plan.max_images_per_product if plan else None
        self.titles_seen = set()
        self.skus_seen = set()


# ── Checking one product ─────────────────────────────────────────────────────

def _money(value, field, errors, required=True):
    if value in (None, ''):
        if required:
            errors[field] = 'Required.'
        return None
    try:
        amount = Decimal(str(value).replace(',', '').replace('GHS', '').replace('₵', '').strip())
    except InvalidOperation:
        errors[field] = f'"{value}" is not a number.'
        return None
    if amount <= 0 or amount >= Decimal('100000000'):
        errors[field] = 'Must be more than 0.'
        return None
    return amount.quantize(Decimal('0.01'))


def _whole(value, field, errors, required=True):
    if value in (None, ''):
        if required:
            errors[field] = 'Required (0 or more).'
        return None
    try:
        number = int(Decimal(str(value).replace(',', '')))
    except InvalidOperation:
        errors[field] = f'"{value}" is not a whole number.'
        return None
    if number < 0:
        errors[field] = 'Can\'t be negative.'
        return None
    return number


def _text_html(value):
    """Plain text from a cell → safe HTML paragraphs (the product page renders HTML)."""
    value = (value or '').strip()
    if not value:
        return ''
    if re.search(r'</?(p|ul|li|br|strong|em)\b', value, re.I):
        from core.html_sanitize import clean_html   # the seller pasted HTML: same allow-list as the editor
        return clean_html(value)
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n|\r\n\r\n', value) if p.strip()]
    return ''.join(f'<p>{html.escape(p).replace(chr(10), "<br>")}</p>' for p in paragraphs)


def _list_html(value):
    items = _split(value)
    if len(items) <= 1:
        return _text_html(value)
    return '<ul>' + ''.join(f'<li>{html.escape(i)}</li>' for i in items) + '</ul>'


def clean_spec(spec, ctx, files=None):
    """
    Check one product (its rows) and return everything needed to create it.
    `files` (table uploads only): {'images': [UploadedFile], 'version_images': {index: UploadedFile}}.
    Raises SpecError.
    """
    errors = {}
    first = spec['product']
    files = files or {}

    title = ' '.join(first.get('title', '').split())
    if not title:
        errors['title'] = 'Required.'
    elif len(title) > 150:
        errors['title'] = 'Keep it under 150 characters.'
    elif title.casefold() in ctx.titles_seen:
        errors['title'] = 'Another product in this upload has the same title.'
    elif Product.objects.filter(vendor=ctx.vendor, title__iexact=title).exists():
        errors['title'] = 'You already have a product with this title.'

    category = None
    raw_category = first.get('category', '')
    if not raw_category:
        errors['category'] = 'Required. Pick one from the category list.'
    else:
        category = ctx.categories.get(raw_category.casefold()) or ctx.categories.get(raw_category.split('›')[-1].strip().casefold())
        if category is None:
            errors['category'] = f'"{raw_category}" is not a category. Copy a code from the category list.'

    brand = None
    if first.get('brand'):
        brand = ctx.brands.get(first['brand'].casefold())
        if brand is None:
            errors['brand'] = f'"{first["brand"]}" is not in the brand list. Leave it empty or pick one from the list.'

    condition = CONDITIONS.get((first.get('condition') or 'new').strip().casefold())
    if condition is None:
        errors['condition'] = 'Use new, used, refurbished, grocery or book.'

    weight = None
    raw_weight = first.get('weight_kg', '')
    if raw_weight in ('', None):
        errors['weight_kg'] = 'Required: the packed weight in kg.'
    else:
        try:
            weight = float(str(raw_weight).replace(',', '.').lower().replace('kg', '').strip())
            if weight <= 0 or weight > 1000:
                raise ValueError
        except ValueError:
            errors['weight_kg'] = f'"{raw_weight}" is not a weight in kg (e.g. 0.5).'

    compare_at = _money(first.get('compare_at_price'), 'compare_at_price', errors, required=False)
    return_days = _whole(first.get('return_days'), 'return_days', errors, required=False) or 0

    barcode = ''
    if first.get('barcode'):
        from catalog.services import clean_gtin
        barcode = clean_gtin(first['barcode']) or ''
        if not barcode:
            errors['barcode'] = 'Use a valid barcode (8, 12, 13 or 14 digits).'

    regions = ctx.default_regions
    if first.get('ships_to'):
        regions, unknown = [], []
        for name in _split(first['ships_to'].replace(',', ';')):
            country = ctx.countries.get(name.casefold())
            (regions.append(country) if country else unknown.append(name))
        if unknown:
            errors['ships_to'] = f'Unknown countr{"y" if len(unknown) == 1 else "ies"}: {", ".join(unknown)}.'
    if not regions and 'ships_to' not in errors:
        errors['ships_to'] = 'Add the countries you ship to (e.g. GH).'

    delivery = ctx.default_delivery
    if first.get('delivery_methods'):
        delivery, unknown = [], []
        for name in _split(first['delivery_methods']):
            method = ctx.delivery.get(name.casefold())
            (delivery.append(method) if method else unknown.append(name))
        if unknown:
            errors['delivery_methods'] = f'Unknown delivery method: {", ".join(unknown)}. Use the names on the upload page.'
    if not delivery and 'delivery_methods' not in errors:
        errors['delivery_methods'] = 'Add at least one delivery method.'

    # ── Versions ──
    axes = []
    names = [first.get(f'option{n}_name', '') for n in range(1, MAX_OPTIONS + 1)]
    try:
        axes = [variations.axis_from_name(n) for n in names if n]
        axes = variations.clean_axes(axes)
    except variations.VariationError as exc:
        errors['options'] = str(exc)

    versions, combos = [], set()
    rows = spec['versions']
    if not axes and len(rows) > 1:
        errors['title'] = errors.get('title') or (
            f'{len(rows)} rows have this title. Give each its own title, or add option columns '
            '(e.g. option1_name = Size) to make them versions of one product.')
    for i, row in enumerate(rows if axes else []):
        label = f'Version {i + 1}'
        row_names = [row.get(f'option{n}_name', '') or names[n - 1] for n in range(1, MAX_OPTIONS + 1)]
        if [n.casefold() for n in row_names if n] != [n.casefold() for n in names if n]:
            errors['options'] = f'{label}: use the same option names on every row of a product ({", ".join(n for n in names if n)}).'
            break
        values = [row.get(f'option{n}_value', '') for n in range(1, len(axes) + 1)]
        try:
            options = variations.clean_variant_options(
                axes, {a['key']: v for a, v in zip(axes, values)}, position=i + 1)
        except variations.VariationError as exc:
            errors['options'] = str(exc)
            break
        combo = variations.combo_key(options)
        if combo in combos:
            errors['options'] = f'Two rows are the same version ({" / ".join(options.values())}).'
            break
        combos.add(combo)
        version_errors = {}
        price = _money(row.get('price') or first.get('price'), 'price', version_errors)
        stock = _whole(row.get('stock'), 'stock', version_errors)
        if version_errors:
            field, message = next(iter(version_errors.items()))
            errors[field] = f'{" / ".join(options.values())}: {message}'
            break
        versions.append({'options': options, 'price': price, 'stock': stock,
                         'seller_sku': (row.get('seller_sku') or '').strip()[:64],
                         'image_url': (row.get('image_url') or '').strip() if i else ''})

    price = stock = None
    if axes:
        price = min(v['price'] for v in versions) if versions else None   # "from" price on listings
        stock = sum(v['stock'] for v in versions)
        product_sku = ''
    else:
        price = _money(first.get('price'), 'price', errors)
        stock = _whole(first.get('stock'), 'stock', errors)
        product_sku = (first.get('seller_sku') or '').strip()[:64]

    # SKUs unique in the store and in this upload.
    skus = [s for s in [product_sku, *(v['seller_sku'] for v in versions)] if s]
    lowered = [s.casefold() for s in skus]
    if len(set(lowered)) != len(lowered) or ctx.skus_seen & set(lowered):
        errors['seller_sku'] = 'Each product and version needs a different SKU.'
    elif skus:
        pattern = r'^(' + '|'.join(map(re.escape, skus)) + r')$'
        if (Product.objects.filter(vendor=ctx.vendor, seller_sku__iregex=pattern).exists()
                or Variants.objects.filter(product__vendor=ctx.vendor, seller_sku__iregex=pattern).exists()):
            errors['seller_sku'] = 'One of these SKUs is already used in your store.'

    # Photos: links (sheet) or files (table).
    image_urls = [u for u in [first.get('image_url', ''), *_split(first.get('more_image_urls', ''))] if u]
    for url in image_urls + [v['image_url'] for v in versions if v['image_url']]:
        if not re.match(r'https?://', url, re.I):
            errors['image_url'] = f'"{url[:60]}" is not a link (it must start with http:// or https://).'
            break
    image_count = len(image_urls) + len(files.get('images') or [])
    if ctx.max_images is not None and image_count > max(ctx.max_images, 1):
        errors['more_image_urls'] = f'Your plan allows {ctx.max_images} photos per product ({image_count} given).'

    if errors:
        raise SpecError(errors)

    ctx.titles_seen.add(title.casefold())
    ctx.skus_seen.update(lowered)
    return {
        'title': title, 'category': category, 'brand': brand, 'condition': condition, 'weight': weight,
        'price': price, 'compare_at': compare_at or Decimal('0'), 'stock': stock, 'return_days': return_days,
        'seller_sku': product_sku, 'barcode': barcode, 'regions': regions, 'delivery': delivery,
        'description': _text_html(first.get('description')), 'features': _list_html(first.get('features')),
        'specifications': _text_html(first.get('specifications')),
        'delivery_returns': _text_html(first.get('delivery_returns')),
        'axes': axes, 'versions': versions, 'image_urls': image_urls, 'files': files,
    }


# ── Creating it ──────────────────────────────────────────────────────────────

def _photo_from_file(upload):
    if upload.size > 10 * 1024 * 1024:
        raise RemoteImageError('The photo is larger than 10 MB.')
    photo, _ = to_product_photo(upload.read())
    return photo


def _gather_photos(cleaned):
    """Download/convert every photo before touching the database. Failures become warnings."""
    warnings = []

    def get(source, what):
        try:
            return _photo_from_file(source) if hasattr(source, 'read') else fetch_photo(source)
        except RemoteImageError as exc:
            warnings.append(f'{what}: {exc}')
        except Exception:   # a broken website must not stop the upload
            logger.exception('bulk upload photo failed')
            warnings.append(f'{what}: the photo could not be added.')
        return None

    sources = list(cleaned['files'].get('images') or []) + cleaned['image_urls']
    product_photos = [p for p in (get(s, 'Photo') for s in sources) if p]
    version_photos = {}
    table_version_files = cleaned['files'].get('version_images') or {}
    for i, version in enumerate(cleaned['versions']):
        source = table_version_files.get(i) or version['image_url']
        if source:
            photo = get(source, f'Photo for {" / ".join(version["options"].values())}')
            if photo:
                version_photos[i] = photo
    # The first row's photo is also the first version's photo (a copy: deleting
    # a variant deletes its file).
    if cleaned['versions'] and 0 not in version_photos and product_photos:
        source = product_photos[0]
        source.seek(0)
        version_photos[0] = ContentFile(source.read(), name=f'{uuid.uuid4().hex}.jpg')
        source.seek(0)
    return product_photos, version_photos, warnings


def create_product(vendor, cleaned):
    """
    Create one checked product. Returns {'product_id', 'status', 'warnings'}.
    Raises payments.entitlements.ProductLimitReached when the plan is full.
    """
    from payments.entitlements import reserve_product_slots

    product_photos, version_photos, warnings = _gather_photos(cleaned)
    main_photo = product_photos[0] if product_photos else (version_photos.get(0) or next(iter(version_photos.values()), None))
    status = 'in_review' if main_photo else 'draft'
    if not main_photo:
        warnings.append('No photo: saved as a draft. Add a photo and submit it from My Products.')

    axes = cleaned['axes']
    with transaction.atomic():
        reserve_product_slots(vendor)
        product = Product(
            vendor=vendor, title=cleaned['title'], sub_category=cleaned['category'], brand=cleaned['brand'],
            product_type=cleaned['condition'], price=cleaned['price'], old_price=cleaned['compare_at'],
            total_quantity=cleaned['stock'], weight=cleaned['weight'], volume=0,
            return_period_days=cleaned['return_days'], seller_sku=cleaned['seller_sku'], gtin=cleaned['barcode'],
            description=cleaned['description'], features=cleaned['features'],
            specifications=cleaned['specifications'], delivery_returns=cleaned['delivery_returns'],
            variant=variations.variant_type_for(axes), variation_axes=axes, status=status,
        )
        if main_photo:
            product.image.save(main_photo.name, main_photo, save=False)
        product.save()
        product.available_in_regions.set(cleaned['regions'])
        for i, method in enumerate(cleaned['delivery']):
            ProductDeliveryOption.objects.create(product=product, delivery_option=method, default=(i == 0))
        for photo in product_photos[1:]:
            extra = ProductImages(product=product)
            extra.images.save(photo.name, photo, save=True)
        for i, version in enumerate(cleaned['versions']):
            variant = Variants(product=product, options=version['options'], price=version['price'],
                               quantity=version['stock'], seller_sku=version['seller_sku'])
            variations.mirror_foreign_keys(variant)
            photo = version_photos.get(i)
            if photo:
                variant.image.save(photo.name, photo, save=False)
            variant.save()
    return {'product_id': product.pk, 'status': status, 'warnings': warnings}


def process_specs(vendor, specs, files_by_spec=None, on_progress=None):
    """
    Check and create every product. Returns the report shared by the sync
    response and the async job: totals, per-product results and errors.
    """
    from payments.entitlements import ProductLimitReached, sync_usage

    ctx = Context(vendor)
    files_by_spec = files_by_spec or {}
    created, errors = [], []
    for index, spec in enumerate(specs):
        row = spec['rows'][0] if spec.get('rows') else index + 1
        title = spec['product'].get('title') or '—'
        try:
            cleaned = clean_spec(spec, ctx, files_by_spec.get(index))
            outcome = create_product(vendor, cleaned)
            created.append({'row': row, 'rows': spec.get('rows', [row]), 'title': cleaned['title'], **outcome})
        except SpecError as exc:
            errors.append({'row': row, 'rows': spec.get('rows', [row]), 'title': title, 'errors': exc.errors})
        except ProductLimitReached as exc:
            errors.append({'row': row, 'rows': spec.get('rows', [row]), 'title': title,
                           'errors': {'plan': str(exc)}})
        except Exception:
            logger.exception('Bulk upload failed for %r (vendor %s)', title, vendor.pk)
            errors.append({'row': row, 'rows': spec.get('rows', [row]), 'title': title,
                           'errors': {'product': 'Something went wrong saving this product. Try it again.'}})
        if on_progress:
            on_progress(len(created), len(errors))
    if created:
        sync_usage(vendor)
    return {
        'total_rows': len(specs),
        'success_count': len(created),
        'failed_count': len(errors),
        'draft_count': sum(1 for c in created if c['status'] == 'draft'),
        'created_product_ids': [c['product_id'] for c in created],
        'products': created,
        'errors': errors,
    }


def specs_need_downloads(specs):
    """True when photos have to be fetched from links (slow: always run as a background job)."""
    for spec in specs:
        for row in [spec['product'], *spec['versions']]:
            links = [row.get('image_url', ''), *_split(row.get('more_image_urls', ''))]
            if any(re.match(r'https?://', link, re.I) for link in links):
                return True
    return False

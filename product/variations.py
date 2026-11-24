"""
Variation options: what a shopper chooses between versions of one product.

A product sold in versions names up to MAX_AXES options (its "variation
theme", as Amazon calls it): Colour, Size, Flavour, Scent, Pack size and so
on, or a name of the seller's own. Each variant stores its value per option
in Variants.options, e.g. {"flavor": "Vanilla", "size": "500 g"}.

Product.variation_axes keeps the options in display order:
    [{"key": "flavor", "label": "Flavour"}, {"key": "size", "label": "Size"}]

Products from before this (Product.variant = Size / Color / Size-Color with
the size and color foreign keys) read the same way through effective_axes()
and variant_options(), so nothing downstream needs two code paths. Colour and
size values are also mirrored onto those foreign keys (colour swatches, size
charts and the colour/size listing filters use them).
"""
import re

from django.utils.text import slugify

CUSTOM_KEY = re.compile(r'x_[a-z0-9_]{1,24}')

MAX_AXES = 3
MAX_VALUE_LENGTH = 40
MAX_LABEL_LENGTH = 30

# kind: how the storefront shows the choice. "swatch" uses the variant photo
# (or the colour's hex code); "button" is a text button.
OPTION_TYPES = [
    {'key': 'color', 'label': 'Colour', 'kind': 'swatch', 'examples': ['Black', 'Navy', 'Red']},
    {'key': 'size', 'label': 'Size', 'kind': 'button', 'examples': ['S', 'M', 'L', 'XL', '42']},
    {'key': 'flavor', 'label': 'Flavour', 'kind': 'button', 'examples': ['Vanilla', 'Chocolate', 'Strawberry']},
    {'key': 'scent', 'label': 'Scent', 'kind': 'button', 'examples': ['Lavender', 'Unscented', 'Citrus']},
    {'key': 'style', 'label': 'Style', 'kind': 'button', 'examples': ['Classic', 'Slim fit', 'Hooded']},
    {'key': 'material', 'label': 'Material', 'kind': 'button', 'examples': ['Cotton', 'Leather', 'Stainless steel']},
    {'key': 'pattern', 'label': 'Pattern', 'kind': 'swatch', 'examples': ['Striped', 'Floral', 'Plain']},
    {'key': 'pack_size', 'label': 'Pack size', 'kind': 'button', 'examples': ['Single', 'Pack of 6', 'Pack of 12']},
    {'key': 'count', 'label': 'Count', 'kind': 'button', 'examples': ['30 tablets', '60 tablets', '100 sheets']},
    {'key': 'weight', 'label': 'Weight', 'kind': 'button', 'examples': ['250 g', '500 g', '1 kg']},
    {'key': 'volume', 'label': 'Volume', 'kind': 'button', 'examples': ['330 ml', '500 ml', '1 L']},
    {'key': 'capacity', 'label': 'Capacity', 'kind': 'button', 'examples': ['64 GB', '128 GB', '256 GB']},
    {'key': 'length', 'label': 'Length', 'kind': 'button', 'examples': ['1 m', '2 m', '18 inches']},
    {'key': 'finish', 'label': 'Finish', 'kind': 'button', 'examples': ['Matte', 'Gloss', 'Brushed']},
    {'key': 'shape', 'label': 'Shape', 'kind': 'button', 'examples': ['Round', 'Square', 'Oval']},
    {'key': 'configuration', 'label': 'Configuration', 'kind': 'button', 'examples': ['Wi-Fi', 'Wi-Fi + Cellular']},
    {'key': 'power', 'label': 'Power', 'kind': 'button', 'examples': ['1000 W', '1500 W', '2000 W']},
    {'key': 'age_range', 'label': 'Age range', 'kind': 'button', 'examples': ['0-6 months', '1-2 years', 'Adult']},
    {'key': 'edition', 'label': 'Edition', 'kind': 'button', 'examples': ['Standard', 'Deluxe']},
]
TYPES_BY_KEY = {t['key']: t for t in OPTION_TYPES}

# Options most sellers of these product types reach for, shown first.
SUGGESTED = {
    'grocery': ['flavor', 'weight', 'volume', 'pack_size', 'count', 'size'],
}
DEFAULT_SUGGESTED = ['color', 'size', 'style', 'material', 'pack_size']

LEGACY_AXES = {
    'Size': ['size'],
    'Color': ['color'],
    'Size-Color': ['size', 'color'],
}


class VariationError(ValueError):
    pass


def axis(key, label=None):
    known = TYPES_BY_KEY.get(key)
    return {
        'key': key,
        'label': label or (known['label'] if known else key.replace('_', ' ').capitalize()),
        'kind': known['kind'] if known else 'button',
    }


def effective_axes(product):
    """The product's options in display order, for old and new products alike."""
    if product.variant == 'None':
        return []
    saved = product.variation_axes or []
    if saved:
        return [axis(a['key'], a.get('label')) for a in saved if a.get('key')]
    return [axis(k) for k in LEGACY_AXES.get(product.variant, [])]


_CATALOGUE_ORDER = {t['key']: i for i, t in enumerate(OPTION_TYPES)}


def variant_options(variant, axes=None):
    """
    {axis key: value} for one variant, in the product's option order. The
    order has to come from the product: jsonb does not keep key order.
    Pass `axes` when the caller already has them (saves loading the product).
    """
    stored = dict(variant.options or {})
    if not stored:
        if variant.size_id and variant.size:
            stored['size'] = variant.size.name
        if variant.color_id and variant.color:
            stored['color'] = variant.color.name
    if axes is None:
        product = variant.product if variant.product_id else None
        axes = effective_axes(product) if product is not None else []
    if axes:
        ordered = {a['key']: stored[a['key']] for a in axes if stored.get(a['key'])}
        if ordered:
            return ordered
    # No options on the product (sold as one version now): a stable order.
    return dict(sorted(((k, v) for k, v in stored.items() if v),
                       key=lambda kv: (_CATALOGUE_ORDER.get(kv[0], 99), kv[0])))


def option_label(variant, axes=None):
    """'Vanilla / 500 g': the variant's choices, for carts, orders and emails."""
    return ' / '.join(variant_options(variant, axes).values())


def variant_type_for(axes):
    """Product.variant for these options; the old values where they fit, so
    older clients (the mobile app before its update) still understand them."""
    keys = [a['key'] for a in axes]
    if not keys:
        return 'None'
    if keys == ['size']:
        return 'Size'
    if keys == ['color']:
        return 'Color'
    if sorted(keys) == ['color', 'size']:
        return 'Size-Color'
    return 'Options'


def _custom_key(label):
    return 'x_' + (slugify(label).replace('-', '_')[:24] or 'option')


def clean_axes(raw):
    """Validate the seller's chosen options. Raises VariationError."""
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise VariationError('Variant options could not be read.')
    if len(raw) > MAX_AXES:
        raise VariationError(f'Choose up to {MAX_AXES} options (for example Colour and Size).')
    axes, seen = [], set()
    for item in raw:
        if isinstance(item, str):
            item = {'key': item}
        if not isinstance(item, dict):
            raise VariationError('Variant options could not be read.')
        key = str(item.get('key') or '').strip()
        label = ' '.join(str(item.get('label') or '').split())[:MAX_LABEL_LENGTH]
        if key not in TYPES_BY_KEY:
            # An option the seller named themselves ("Strength", "Grind"). The
            # form sends its own x_ key (variants refer to it); otherwise derive one.
            if not label:
                raise VariationError('Give your own option a name.')
            if not CUSTOM_KEY.fullmatch(key):
                key = _custom_key(label)
        if key in seen:
            raise VariationError(f'"{label or TYPES_BY_KEY[key]["label"]}" is chosen twice.')
        seen.add(key)
        axes.append({'key': key, 'label': label or TYPES_BY_KEY[key]['label']})
    return axes


_NAME_ALIASES = {'colour': 'color', 'flavour': 'flavor', 'pack': 'pack_size', 'pack size': 'pack_size',
                 'age': 'age_range', 'age range': 'age_range', 'storage': 'capacity'}


def axis_from_name(name):
    """
    An option typed as a word (a spreadsheet column, 'Flavor', 'colour', 'Roast level')
    as {key, label}: a known type when it matches one, otherwise the seller's own.
    """
    label = ' '.join(str(name or '').split())[:MAX_LABEL_LENGTH]
    if not label:
        raise VariationError('Give the option a name, for example Colour or Flavour.')
    low = label.casefold()
    key = _NAME_ALIASES.get(low) or low.replace(' ', '_')
    if key in TYPES_BY_KEY:
        return {'key': key, 'label': TYPES_BY_KEY[key]['label']}
    for t in OPTION_TYPES:
        if t['label'].casefold() == low:
            return {'key': t['key'], 'label': t['label']}
    return {'key': _custom_key(label), 'label': label}


def clean_value(value):
    return ' '.join(str(value or '').split())[:MAX_VALUE_LENGTH]


def clean_variant_options(axes, raw, *, position):
    """
    The variant's value for every option, trimmed. `position` (1-based) is
    only for messages. Raises VariationError.
    """
    raw = raw if isinstance(raw, dict) else {}
    options = {}
    for a in axes:
        value = clean_value(raw.get(a['key']))
        if not value:
            raise VariationError(f'Variant {position}: choose a {a["label"].lower()}.')
        options[a['key']] = value
    return options


def combo_key(options):
    return tuple(sorted((k, v.casefold()) for k, v in options.items()))


def mirror_foreign_keys(variant):
    """Point size/color at the matching Size/Color rows (created if new)."""
    from product.models import Color, Size

    options = variant.options or {}
    for field, model in (('size', Size), ('color', Color)):
        value = (options.get(field) or '').strip()
        if not value:
            setattr(variant, field, None)
            continue
        current = getattr(variant, field)
        if current is not None and current.name.casefold() == value.casefold():
            continue
        name_max = model._meta.get_field('name').max_length
        row = model.objects.filter(name__iexact=value[:name_max]).order_by('pk').first()
        if row is None:
            row = model.objects.create(name=value[:name_max])
        setattr(variant, field, row)


# ── Storefront selector ──────────────────────────────────────────────────────

def _colour_code(variant):
    if variant.color_id and variant.color and variant.color.code:
        return variant.color.code.strip() or None
    return None


def selector(product, current, variants, image_url=lambda v: None, price_of=lambda v: None):
    """
    One entry per option for the product page, each value pointing at the
    variant to open when it is clicked:

        [{"key": "flavor", "label": "Flavour", "kind": "button", "selected": "Vanilla",
          "values": [{"value": "Vanilla", "variant_id": 4, "in_stock": true, "available": true,
                      "selected": true, "image": "...", "swatch": null, "price": 12.5, "price_from": false}]}]

    The rows cascade, like Walmart and Amazon: the first option (in the order
    the seller set) lists every value; each later row lists only the values
    sold with the choices in the rows above it. So with Flavour then Pack
    size, picking Barbecue shows only the packs Barbecue comes in.

    Clicking a value keeps the shopper's choices in the other rows where such
    a version exists, preferring one in stock; otherwise it opens the closest
    version with that value. "price" is that value's price, or the lowest of
    several ("price_from") when the rows below still change it.
    """
    axes = effective_axes(product)
    if not axes or current is None:
        return []
    rows = [(v, variant_options(v, axes)) for v in variants]
    chosen = variant_options(current, axes)

    def score(opts, v, key):
        # How many of the shopper's other choices this variant keeps, then stock.
        same = sum(1 for a in axes if a['key'] != key and opts.get(a['key']) == chosen.get(a['key']))
        return (same, v.quantity > 0, -v.pk)

    result = []
    for index, a in enumerate(axes):
        key = a['key']
        above = [b['key'] for b in axes[:index]]
        # Only versions sold with the shopper's choices in the rows above.
        pool = [(v, opts) for v, opts in rows if all(opts.get(k) == chosen.get(k) for k in above)] or rows
        values, order = {}, []
        for v, opts in pool:
            value = opts.get(key)
            if not value:
                continue
            if value not in values:
                values[value] = []
                order.append(value)
            values[value].append((v, opts))
        entries = []
        for value in order:
            candidates = values[value]
            best_v, _ = max(candidates, key=lambda c: score(c[1], c[0], key))
            prices = {p for p in (price_of(v) for v, _ in candidates) if p is not None}
            entries.append({
                'value': value,
                'variant_id': best_v.pk,
                'available': True,
                'in_stock': any(v.quantity > 0 for v, _ in candidates),
                'selected': chosen.get(key) == value,
                'image': image_url(best_v) if a['kind'] == 'swatch' else None,
                'swatch': _colour_code(best_v) if key == 'color' else None,
                'price': min(prices) if prices else None,
                'price_from': len(prices) > 1,
            })
        result.append({**a, 'selected': chosen.get(key), 'values': entries})
    return result


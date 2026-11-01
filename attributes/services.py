"""
Reading, writing, filtering and counting attribute values.

Filter URL format (listing pages):
    ?a_ram=8-gb,16-gb      choice: any of these options
    ?a_screen_min=6&a_screen_max=7   number range
    ?a_waterproof=1        boolean
Different attributes AND together; options within one attribute OR.
"""
import hashlib
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q

from .models import AttributeDefinition, AttributeOption, ProductAttributeValue

DEFS_CACHE_SECONDS = 600
FACETS_CACHE_SECONDS = 300
MAX_OPTIONS_PER_FACET = 40


class AttributeValueError(Exception):
    pass


# ── Which attributes apply ───────────────────────────────────────────────────

def definitions_for(sub_category):
    """Definitions for a sub-category (own + parent category's), with options. Cached."""
    if sub_category is None:
        return []
    key = f"attr_defs:{sub_category.pk}"
    defs = cache.get(key)
    if defs is None:
        defs = list(
            AttributeDefinition.objects
            .filter(Q(sub_categories=sub_category) | Q(categories=sub_category.category_id))
            .distinct().prefetch_related('options').order_by('position', 'name')
        )
        cache.set(key, defs, DEFS_CACHE_SECONDS)
    return defs


def clear_definitions_cache():
    """Called when staff edit definitions (admin save)."""
    from product.models import Sub_Category
    cache.delete_many([f"attr_defs:{pk}" for pk in Sub_Category.objects.values_list('pk', flat=True)])


def definition_json(d):
    return {
        'id': d.pk, 'name': d.name, 'slug': d.slug, 'type': d.type, 'unit': d.unit, 'multi': d.multi,
        'filterable': d.filterable, 'required': d.required, 'help_text': d.help_text,
        'options': [{'id': o.pk, 'value': o.value, 'slug': o.slug} for o in d.options.all()]
        if d.type == AttributeDefinition.CHOICE else [],
    }


# ── Product values ───────────────────────────────────────────────────────────

def specs_for(product_ids):
    """{product_id: [{'name', 'slug', 'value'}...]} in definition order (multi-choice joined)."""
    rows = (ProductAttributeValue.objects.filter(product_id__in=product_ids)
            .select_related('definition', 'option')
            .order_by('definition__position', 'definition__name', 'option__position'))
    out = {}
    for v in rows:
        text = v.display()
        if text == '':
            continue
        specs = out.setdefault(v.product_id, OrderedDict())
        entry = specs.setdefault(v.definition.slug, {'name': v.definition.name, 'slug': v.definition.slug, 'values': []})
        entry['values'].append(text)
    return {pid: [{'name': e['name'], 'slug': e['slug'], 'value': ', '.join(e['values'])} for e in specs.values()]
            for pid, specs in out.items()}


def values_for_edit(product):
    """{slug: value} for the seller form: option ids (list for multi), number, bool or text."""
    out = {}
    for v in ProductAttributeValue.objects.filter(product=product).select_related('definition'):
        d = v.definition
        if d.type == AttributeDefinition.CHOICE:
            if d.multi:
                out.setdefault(d.slug, []).append(v.option_id)
            else:
                out[d.slug] = v.option_id
        elif d.type == AttributeDefinition.NUMBER:
            out[d.slug] = v.value_number
        elif d.type == AttributeDefinition.BOOLEAN:
            out[d.slug] = v.value_bool
        else:
            out[d.slug] = v.value_text
    return out


def _coerce(d, raw, options_by_id):
    """Rows (kwargs) for one definition from a submitted value, or raise AttributeValueError."""
    if raw is None or raw == '' or raw == []:
        return []
    if d.type == AttributeDefinition.CHOICE:
        ids = raw if isinstance(raw, list) else [raw]
        if not d.multi and len(ids) > 1:
            raise AttributeValueError(f"{d.name}: choose one option.")
        rows = []
        for i in ids:
            try:
                opt = options_by_id[int(i)]
            except (KeyError, TypeError, ValueError):
                raise AttributeValueError(f"{d.name}: that option isn't available.")
            rows.append({'option': opt})
        return rows
    if d.type == AttributeDefinition.NUMBER:
        try:
            num = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            raise AttributeValueError(f"{d.name}: enter a number.")
        if not num.is_finite() or abs(num) >= Decimal('1e10'):
            raise AttributeValueError(f"{d.name}: enter a realistic number.")
        return [{'value_number': num}]
    if d.type == AttributeDefinition.BOOLEAN:
        if not isinstance(raw, bool):
            raise AttributeValueError(f"{d.name}: choose yes or no.")
        return [{'value_bool': raw}]
    text = ' '.join(str(raw).split())[:255]
    return [{'value_text': text}] if text else []


@transaction.atomic
def save_values(product, data):
    """
    Replace the product's values for the attributes present in `data` ({slug: value}).
    Attributes not in `data` are left alone; unknown slugs are refused.
    Returns the list of required attributes still missing.
    """
    defs = {d.slug: d for d in definitions_for(product.sub_category)}
    unknown = [slug for slug in data if slug not in defs]
    if unknown:
        raise AttributeValueError(f"These don't apply to this category: {', '.join(unknown[:5])}.")
    new_rows, touched = [], []
    for slug, raw in data.items():
        d = defs[slug]
        options = {o.pk: o for o in d.options.all()}
        for kwargs in _coerce(d, raw, options):
            new_rows.append(ProductAttributeValue(product=product, definition=d, **kwargs))
        touched.append(d.pk)
    ProductAttributeValue.objects.filter(product=product, definition_id__in=touched).delete()
    ProductAttributeValue.objects.bulk_create(new_rows)
    refresh_search_vector(product.pk)
    from django.core.cache import cache as _cache
    from product.signals import product_detail_cache_key
    transaction.on_commit(lambda: _cache.delete(product_detail_cache_key(product)))
    return missing_required(product, defs.values())


def missing_required(product, defs=None):
    defs = list(defs if defs is not None else definitions_for(product.sub_category))
    have = set(ProductAttributeValue.objects.filter(product=product).values_list('definition_id', flat=True))
    return [d.slug for d in defs if d.required and d.pk not in have]


def search_text(product_id):
    """Attribute values as words for full-text search ("8 GB", "Cotton")."""
    return ' '.join(row['value'] for row in specs_for([product_id]).get(product_id, []))


def refresh_search_vector(product_id):
    from django.contrib.postgres.search import SearchVector
    from django.db.models import F, Value

    from product.models import SEARCH_CONFIG, Product
    Product.objects.filter(pk=product_id).update(search_vector=(
        SearchVector(F('title'), weight='A', config=SEARCH_CONFIG)
        + SearchVector(F('description'), weight='B', config=SEARCH_CONFIG)
        + SearchVector(F('features'), weight='C', config=SEARCH_CONFIG)
        + SearchVector(F('specifications'), weight='C', config=SEARCH_CONFIG)
        + SearchVector(Value(search_text(product_id)), weight='B', config=SEARCH_CONFIG)
    ))


# ── Listing filters and facets ───────────────────────────────────────────────

def parse_filters(params, defs):
    """{definition: filter} from query params; bad values are ignored, not errors."""
    by_slug = {d.slug: d for d in defs}
    active = {}
    for d in by_slug.values():
        if not d.filterable:
            continue
        if d.type == AttributeDefinition.CHOICE:
            raw = params.get(f'a_{d.slug}')
            if raw:
                wanted = {s for s in raw.split(',') if s}
                ids = [o.pk for o in d.options.all() if o.slug in wanted]
                if ids:
                    active[d] = {'options': ids, 'slugs': sorted(wanted)}
        elif d.type == AttributeDefinition.NUMBER:
            rng = {}
            for bound in ('min', 'max'):
                try:
                    rng[bound] = Decimal(params[f'a_{d.slug}_{bound}'])
                except (KeyError, InvalidOperation, ValueError):
                    pass
            if rng:
                active[d] = rng
        elif d.type == AttributeDefinition.BOOLEAN:
            raw = params.get(f'a_{d.slug}')
            if raw in ('1', 'true', '0', 'false'):
                active[d] = {'bool': raw in ('1', 'true')}
    return active


def _subquery(d, f):
    qs = ProductAttributeValue.objects.filter(definition=d)
    if 'options' in f:
        qs = qs.filter(option_id__in=f['options'])
    elif 'bool' in f:
        qs = qs.filter(value_bool=f['bool'])
    else:
        if 'min' in f:
            qs = qs.filter(value_number__gte=f['min'])
        if 'max' in f:
            qs = qs.filter(value_number__lte=f['max'])
    return qs.values('product_id')


def apply_filters(qs, active, skip=None):
    for d, f in active.items():
        if d is not skip:
            qs = qs.filter(id__in=_subquery(d, f))
    return qs


def facets(base_qs, defs, active, cache_key=None):
    """
    Filter groups with counts for the sidebar. Each group counts against the
    listing with every *other* attribute filter applied (so picking "8 GB"
    still shows how many "16 GB" there are). One GROUP BY for all choice
    attributes, plus one per active choice filter and one per number/boolean
    attribute. Cached briefly per filter combination.
    """
    filterable = [d for d in defs if d.filterable and d.type != AttributeDefinition.TEXT]
    if not filterable:
        return []
    if cache_key:
        digest = hashlib.md5(repr(sorted((d.slug, repr(f)) for d, f in active.items())).encode()).hexdigest()
        key = f"attr_facets:{cache_key}:{digest}"
        hit = cache.get(key)
        if hit is not None:
            return hit

    filtered_ids = apply_filters(base_qs, active).order_by().values('id')
    choice_defs = [d for d in filterable if d.type == AttributeDefinition.CHOICE]

    counts = {}
    plain = [d.pk for d in choice_defs if d not in active]
    if plain:
        for opt_id, n in (ProductAttributeValue.objects.filter(definition_id__in=plain, product_id__in=filtered_ids)
                          .values_list('option_id').annotate(n=Count('product_id', distinct=True))):
            counts[opt_id] = n
    for d in choice_defs:
        if d in active:
            ids = apply_filters(base_qs, active, skip=d).order_by().values('id')
            for opt_id, n in (ProductAttributeValue.objects.filter(definition=d, product_id__in=ids)
                              .values_list('option_id').annotate(n=Count('product_id', distinct=True))):
                counts[opt_id] = n

    groups = []
    for d in filterable:
        f = active.get(d, {})
        if d.type == AttributeDefinition.CHOICE:
            opts = [{'value': o.value, 'slug': o.slug, 'count': counts.get(o.pk, 0),
                     'selected': o.pk in f.get('options', [])} for o in d.options.all()]
            opts = [o for o in opts if o['count'] or o['selected']][:MAX_OPTIONS_PER_FACET]
            if opts:
                groups.append({'slug': d.slug, 'name': d.name, 'type': d.type, 'unit': d.unit, 'options': opts})
        elif d.type == AttributeDefinition.NUMBER:
            from django.db.models import Max, Min
            ids = apply_filters(base_qs, active, skip=d).order_by().values('id')
            agg = ProductAttributeValue.objects.filter(definition=d, product_id__in=ids).aggregate(
                lo=Min('value_number'), hi=Max('value_number'))
            if agg['lo'] is not None and agg['lo'] != agg['hi']:
                groups.append({'slug': d.slug, 'name': d.name, 'type': d.type, 'unit': d.unit,
                               'min': agg['lo'].normalize(), 'max': agg['hi'].normalize(),
                               'selected_min': f.get('min'), 'selected_max': f.get('max')})
        else:
            ids = apply_filters(base_qs, active, skip=d).order_by().values('id')
            yes = ProductAttributeValue.objects.filter(definition=d, value_bool=True, product_id__in=ids).count()
            if yes:
                groups.append({'slug': d.slug, 'name': d.name, 'type': d.type, 'unit': '',
                               'count': yes, 'selected': f.get('bool') is True})
    if cache_key:
        cache.set(key, groups, FACETS_CACHE_SECONDS)
    return groups

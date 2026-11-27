"""
Listing filters for variant options: Flavour, Scent, Pack size, Material…
and the options sellers name themselves (x_fabric, x_strength…), next to
the colour / size / brand filters every listing already has.

URL:  ?o_<key>=<value>,<value>   e.g. ?o_flavor=vanilla,chocolate&o_x_fabric=silk
      Values match any capitals and spacing ("Pack of 6" = "pack  of 6").

Facets come back in the listing's "attributes" list, in the same shape as
the category specs (attributes/services.facets), so the storefront's filter
drawer shows them without a special case:
    {"slug": "o_flavor", "param": "o_flavor", "name": "Flavour", "type": "choice",
     "options": [{"slug": "vanilla", "value": "Vanilla", "count": 12}, ...]}

Counts follow the usual rule: each option group counts the products the
other filters leave, so ticking Vanilla doesn't hide Chocolate.

Colour and size stay on their own filters (the Size/Color rows mirrored by
product/variations.py), so they are not repeated here. Sellers who name the
same option ("Fabric") get the same key (x_fabric), so stores merge.
"""
import hashlib
import re

from django.core.cache import cache
from django.core.exceptions import EmptyResultSet
from django.db import connection
from django.db.models import CharField, Func, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Lower, Trim

from product import variations

PREFIX = 'o_'
KEY = re.compile(r'^[a-z0-9_]{1,30}$')
OWN_FILTERS = ('color', 'size')        # have their own listing filters
MAX_KEYS = 6                           # selected option groups per request
MAX_VALUES = 20                        # selected values per group
SHOW_KEYS = 8                          # option groups shown in the drawer
SHOW_VALUES = 40                       # values shown per group
CACHE_TTL = 600


def normalise(value):
    """'  Pack of 6, Large ' → 'pack of 6 large' (commas separate values in the URL)."""
    return re.sub(r'[\s,]+', ' ', str(value or '').lower()).strip()[:variations.MAX_VALUE_LENGTH]


def parse(params):
    """{key: [normalised values]} from ?o_<key>=a,b (repeated params work too)."""
    active = {}
    for name in params.keys():
        if not name.startswith(PREFIX):
            continue
        key = name[len(PREFIX):]
        if not KEY.match(key) or key in OWN_FILTERS:
            continue
        values = []
        for raw in params.getlist(name):
            for part in str(raw).split(','):
                value = normalise(part)
                if value and value not in values:
                    values.append(value)
        if values:
            active[key] = values[:MAX_VALUES]
        if len(active) >= MAX_KEYS:
            break
    return active


def cache_part(active):
    """A short, cache-safe token for the selected options ('' when none)."""
    if not active:
        return ''
    return hashlib.md5(repr(sorted(active.items())).encode()).hexdigest()[:16]


def _normalised_option(key):
    """SQL for the variant's value of `key`, normalised like normalise()."""
    return Trim(Func(
        Lower(KeyTextTransform(key, 'options')), Value(r'[\s,]+'), Value(' '), Value('g'),
        function='regexp_replace', output_field=CharField(),
    ))


def matching_product_ids(key, values):
    """Product ids with a version whose `key` option is one of `values`."""
    from product.models import Variants
    return (Variants.objects.filter(options__has_key=key)          # uses variant_options_gin
            .annotate(_option=_normalised_option(key))
            .filter(_option__in=values).values('product_id'))


def apply(queryset, active, skip=None):
    """Keep products that have every selected option (any of its values)."""
    for key, values in active.items():
        if key != skip:
            queryset = queryset.filter(id__in=matching_product_ids(key, values))
    return queryset


def _subquery(queryset):
    """SQL for the queryset's product ids, or None when it can match nothing (id__in=[])."""
    try:
        return queryset.order_by().values('id').query.sql_with_params()
    except EmptyResultSet:
        return None


def _rows(queryset, only_key=None):
    """
    ([(key, normalised, shown value, products)], {key: products}) for the
    products in `queryset`. The second part counts each product once per key.
    """
    from product.models import Variants
    sub = _subquery(queryset)
    if sub is None:
        return [], {}
    sub_sql, sub_params = sub
    sql = f"""
        SELECT key, norm,
               mode() WITHIN GROUP (ORDER BY raw COLLATE "C") AS shown,
               COUNT(DISTINCT product_id) AS products
        FROM (
            SELECT v.product_id, kv.key, btrim(kv.value) AS raw,
                   btrim(regexp_replace(lower(kv.value), '[\\s,]+', ' ', 'g')) AS norm
            FROM {Variants._meta.db_table} v
            CROSS JOIN LATERAL jsonb_each_text(
                CASE WHEN jsonb_typeof(v.options) = 'object' THEN v.options ELSE '{{}}'::jsonb END) AS kv
            WHERE v.product_id IN ({sub_sql})
              AND kv.key <> ALL(%s)
              {"AND kv.key = %s" if only_key else ""}
              AND btrim(kv.value) <> ''
        ) t
        GROUP BY GROUPING SETS ((key, norm), (key))
    """
    params = [*sub_params, list(OWN_FILTERS)] + ([only_key] if only_key else [])
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        fetched = cursor.fetchall()
    rows = [r for r in fetched if r[1] is not None]
    totals = {r[0]: r[3] for r in fetched if r[1] is None}
    return rows, totals


def _labels(queryset, keys):
    """What shoppers call each option: the name most sellers gave it."""
    from product.models import Product
    labels = {k: variations.axis(k)['label'] for k in keys}
    custom = [k for k in keys if k not in variations.TYPES_BY_KEY]
    sub = _subquery(queryset) if custom else None
    if sub is None:
        return labels
    sub_sql, sub_params = sub
    sql = f"""
        SELECT a->>'key', a->>'label', COUNT(*) AS n
        FROM {Product._meta.db_table} p
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(p.variation_axes) = 'array' THEN p.variation_axes ELSE '[]'::jsonb END) AS a
        WHERE p.id IN ({sub_sql}) AND a->>'key' = ANY(%s)
        GROUP BY 1, 2
        ORDER BY n DESC
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [*sub_params, custom])
        seen = set()
        for key, label, _ in cursor.fetchall():
            if key not in seen and label:
                labels[key] = label
                seen.add(key)
    return labels


def facets(queryset, active, cache_key=None):
    """
    Option groups for the filter drawer. `queryset` has every other filter
    applied but no option filters (apply() is done here per group).
    """
    if cache_key:
        key = 'option_facets:' + hashlib.md5(f"{cache_key}|{cache_part(active)}".encode()).hexdigest()
        cached = cache.get(key)
        if cached is not None:
            return cached

    groups, totals = {}, {}
    # Groups nobody has ticked: count within everything selected.
    rows, all_totals = _rows(apply(queryset, active))
    for k, norm, shown, n in rows:
        if k not in active:
            groups.setdefault(k, []).append((norm, shown, n))
            totals[k] = all_totals.get(k, 0)
    # A ticked group: count within the other selections, so its other values stay.
    for k in active:
        rows, key_totals = _rows(apply(queryset, active, skip=k), only_key=k)
        groups[k] = [(norm, shown, n) for _, norm, shown, n in rows]
        totals[k] = key_totals.get(k, 0)

    # Worth showing: ticked, or offered by at least two products.
    keys = [k for k in groups if k in active or totals[k] >= 2]
    keys.sort(key=lambda k: (k not in active, -totals[k], k))
    keys = keys[:SHOW_KEYS]
    labels = _labels(queryset, keys) if keys else {}

    result = []
    for k in keys:
        selected = set(active.get(k, ()))
        values = sorted(groups[k], key=lambda row: (row[0] not in selected, -row[2], row[0]))[:SHOW_VALUES]
        for missing in selected - {row[0] for row in values}:      # ticked, now with no matches
            values.append((missing, missing, 0))
        result.append({
            'slug': f'{PREFIX}{k}', 'param': f'{PREFIX}{k}', 'name': labels.get(k, k), 'type': 'choice',
            'kind': 'option',
            'options': [{'slug': norm, 'value': shown, 'count': n} for norm, shown, n in values],
        })

    if cache_key:
        cache.set(key, result, CACHE_TTL)
    return result

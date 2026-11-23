"""
tools/listing_doctor.py
How good is each listing, and help writing a better one.

score(): a 0-100 quality score with the reasons points were lost, computed
from the listing itself (no AI, no cost), each reason pointing at the product
form section that fixes it. Built for whole pages of listings: photo counts,
spec values and versions are prefetched, never queried per product.

suggest(): an AI rewrite of the title, description and key features from what
the seller already wrote. Uses one unit of the plan's monthly allowance
(tools.models.ToolUsage), given back if the call fails. Nothing is saved until
the seller applies it (apply_suggestion).
"""
import html
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Prefetch

from product.models import Product, Variants
from product.signals import product_detail_cache_key
from product.variations import effective_axes, variant_options

logger = logging.getLogger(__name__)

PAGE_SIZE = 30
PROMO_WORDS = re.compile(r'\b(best price|cheapest|free shipping|sale|discount|call|whatsapp|promo|hot deal)\b', re.I)
PHONE = re.compile(r'(\+?\d[\d\s-]{8,}\d)')


def plain(html_text):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html_text or '')).strip()


# ── Score ────────────────────────────────────────────────────────────────────

def _issue(points, section, message):
    return {'points': points, 'section': section, 'message': message}


def score_product(p, photo_count, spec_count, required_specs):
    """(score, issues) for one product. photo_count includes the main photo."""
    issues = []
    if not p.image:
        issues.append(_issue(15, 'media', 'Add a main photo: listings without one can\'t go live.'))
    if photo_count < 3:
        issues.append(_issue(10, 'media', f'Add more photos ({photo_count} now): show other angles, the label and size.'))

    title = ' '.join((p.title or '').split())
    if len(title) < 25:
        issues.append(_issue(10, 'basics', 'Make the title more descriptive: brand, product, key detail (25+ characters).'))
    elif title.isupper():
        issues.append(_issue(5, 'basics', 'Write the title in normal case, not all capitals.'))
    if PROMO_WORDS.search(title) or PHONE.search(title):
        issues.append(_issue(5, 'basics', 'Remove prices, promotions and phone numbers from the title.'))

    words = len(plain(p.description).split())
    if words < 15:
        issues.append(_issue(15, 'description', 'Write a description: what it is, who it\'s for, what\'s in the box.'))
    elif words < 50:
        issues.append(_issue(7, 'description', f'Say more in the description ({words} words now; aim for 50+).'))
    if not plain(p.features):
        issues.append(_issue(10, 'description', 'List the key features shoppers compare.'))

    if required_specs:
        missing = max(required_specs - spec_count, 0)
        if missing:
            issues.append(_issue(round(10 * missing / required_specs), 'specs',
                                 f'Fill in the specs shoppers filter by ({missing} missing).'))
    if not p.brand_id:
        issues.append(_issue(5, 'basics', 'Choose the brand (or "Generic" if it has none).'))
    if not p.weight or p.weight <= 0:
        issues.append(_issue(5, 'pricing', 'Add the packed weight so delivery is priced right.'))
    if not p.return_period_days:
        issues.append(_issue(5, 'more', 'Offer returns: listings with a return window sell better.'))
    if p.variant != 'None':
        versions = list(p.variants.all())
        no_photo = sum(1 for v in versions if not v.image or v.image.name == 'product.jpg')
        if versions and no_photo:
            issues.append(_issue(5, 'variants', f'Add a photo for each version ({no_photo} without one).'))
    lost = sum(i['points'] for i in issues)
    issues.sort(key=lambda i: -i['points'])
    return max(100 - lost, 0), issues


SCORES_CACHE_SECONDS = 300


def _scores_key(vendor):
    return f'tools:listing_scores:{vendor.pk}'


def page(vendor, number=1, order='worst'):
    """A page of listings with scores, worst first by default, plus the store average."""
    rows = cache.get(_scores_key(vendor))
    if rows is None:
        rows = _all_scores(vendor)
        cache.set(_scores_key(vendor), rows, SCORES_CACHE_SECONDS)
    if order != 'worst':
        rows = sorted(rows, key=lambda r: (-r['score'], r['product_id']))
    total = len(rows)
    start = (max(number, 1) - 1) * PAGE_SIZE
    return {
        'count': total, 'page': max(number, 1), 'page_size': PAGE_SIZE,
        'average': round(sum(r['score'] for r in rows) / total) if total else None,
        'needs_work': sum(1 for r in rows if r['score'] < 70),
        'results': rows[start:start + PAGE_SIZE],
    }


def _all_scores(vendor):
    """Every listing scored, worst first (cached by page())."""
    from attributes.services import definitions_for

    qs = (Product.objects.filter(vendor=vendor).exclude(status__in=['rejected', 'disabled'])
          .annotate(photo_extra=Count('p_images', distinct=True), spec_count=Count('attribute_values__definition',
                                                                                    distinct=True))
          .select_related('sub_category')
          .prefetch_related(Prefetch('variants', queryset=Variants.objects.only('pk', 'product_id', 'image'))))
    products = list(qs)            # scores are computed in Python, so sort after scoring
    required_by_category = {}
    scored = []
    for p in products:
        if p.sub_category_id not in required_by_category:
            required_by_category[p.sub_category_id] = sum(1 for d in definitions_for(p.sub_category) if d.required)
        photos = (1 if p.image else 0) + p.photo_extra
        value, issues = score_product(p, photos, p.spec_count, required_by_category[p.sub_category_id])
        scored.append((value, p, issues))
    scored.sort(key=lambda s: (s[0], s[1].pk))
    return [{'product_id': p.pk, 'title': p.title, 'sku': p.sku, 'slug': p.slug, 'status': p.status,
             'image': p.image.url if p.image else None, 'score': value, 'issues': issues}
            for value, p, issues in scored]


# ── AI suggestion ────────────────────────────────────────────────────────────

class AssistantUnavailable(Exception):
    """Message is safe to show to the seller."""


SYSTEM = (
    "You write product listings for Negromart, an online marketplace in Ghana. Shoppers read on phones and "
    "compare listings quickly. Write in clear, simple British English. Use only facts present in the seller's "
    "information; never invent specifications, certifications, materials, warranties, sizes or claims. If a "
    "detail is missing, leave it out rather than guess. Titles: brand, product type, key distinguishing "
    "detail, 25-120 characters, normal capitalisation, no prices, promotions, emojis or phone numbers, and no "
    "colour or size when the product is sold in several. Descriptions: 2-4 short paragraphs saying what it is, "
    "who it is for, how it is used and what is in the box when known. Features: 3-7 short, concrete bullet "
    "points shoppers compare."
)


def _client():
    import anthropic
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or ''
    if not api_key:
        raise AssistantUnavailable('The writing assistant is not set up yet. Please try again later.')
    return anthropic.Anthropic(api_key=api_key, timeout=90.0, max_retries=2)


def _facts(product):
    from attributes.models import ProductAttributeValue

    specs = []
    for v in (ProductAttributeValue.objects.filter(product=product)
              .select_related('definition', 'option').order_by('definition__position')[:40]):
        value = (v.option.value if v.option_id else v.value_text or
                 (('Yes' if v.value_bool else 'No') if v.value_bool is not None else
                  (f'{v.value_number.normalize()} {v.definition.unit or ""}'.strip() if v.value_number is not None else '')))
        if value:
            specs.append(f'{v.definition.name}: {value}')
    axes = effective_axes(product)
    versions = sorted({' / '.join(variant_options(v, axes).values()) for v in product.variants.all()[:30]} - {''})
    category = product.sub_category.title if product.sub_category_id else ''
    return '\n'.join(line for line in [
        f'Current title: {product.title}',
        f'Category: {category}' if category else '',
        f'Brand: {product.brand.title}' if product.brand_id else '',
        f'Condition: {product.product_type}' if product.product_type else '',
        f'Sold in versions: {", ".join(versions)}' if versions else '',
        f'Specifications: {"; ".join(specs)}' if specs else '',
        f'Current description: {plain(product.description)[:3000]}' if plain(product.description) else '',
        f'Current features: {plain(product.features)[:1500]}' if plain(product.features) else '',
    ] if line)


def suggest(vendor, product, notes=''):
    """
    Ask Claude for a better title, description and features. Uses one unit of
    the monthly allowance; gives it back if anything fails. Raises
    AssistantUnavailable (allowance used up, not configured, or the call failed).
    """
    from pydantic import BaseModel, Field

    import anthropic
    from payments.entitlements import plan_for_vendor
    from tools import registry
    from tools.models import ToolUsage

    class Listing(BaseModel):
        title: str = Field(description='The improved title')
        description_paragraphs: list[str] = Field(description='2-4 short paragraphs')
        features: list[str] = Field(description='3-7 short bullet points')

    limit = registry.allowance(plan_for_vendor(vendor), 'listing_doctor')
    client = _client()          # before taking a unit: a missing key must not cost the seller
    if not ToolUsage.take(vendor, 'listing_doctor', limit):
        raise AssistantUnavailable(f'You have used all {limit} AI rewrites for this month. They renew on the 1st.')
    request = _facts(product)
    if notes:
        request += f'\nSeller\'s notes: {notes[:500]}'
    try:
        response = client.beta.messages.parse(
            model=getattr(settings, 'LISTING_ASSISTANT_MODEL', 'claude-opus-5-5'),
            max_tokens=16000,
            output_config={'effort': getattr(settings, 'LISTING_ASSISTANT_EFFORT', 'medium')},
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
            system=SYSTEM,
            messages=[{'role': 'user', 'content': f'Improve this listing.\n\n{request}'}],
            output_format=Listing,
        )
        if response.stop_reason == 'refusal' or response.parsed_output is None:
            raise AssistantUnavailable('The assistant couldn\'t rewrite this listing. Try adding a few notes.')
        result = response.parsed_output
    except AssistantUnavailable:
        ToolUsage.give_back(vendor, 'listing_doctor')
        raise
    except anthropic.RateLimitError:
        ToolUsage.give_back(vendor, 'listing_doctor')
        raise AssistantUnavailable('The assistant is busy right now. Please try again in a minute.')
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        ToolUsage.give_back(vendor, 'listing_doctor')
        logger.warning('Listing assistant failed for product %s: %s', product.pk, exc)
        raise AssistantUnavailable('The assistant couldn\'t be reached. Please try again.')

    paragraphs = [p.strip() for p in result.description_paragraphs if p.strip()][:6]
    features = [f.strip() for f in result.features if f.strip()][:10]
    return {
        'title': ' '.join(result.title.split())[:150],
        'description': ''.join(f'<p>{html.escape(p)}</p>' for p in paragraphs),
        'features': '<ul>' + ''.join(f'<li>{html.escape(f)}</li>' for f in features) + '</ul>' if features else '',
        'used': ToolUsage.used(vendor, 'listing_doctor'),
        'allowance': limit,
    }


def apply_suggestion(vendor, product, title=None, description=None, features=None):
    """Save the parts of a suggestion the seller accepted (cleaned like editor input)."""
    from core.html_sanitize import clean_html

    fields = []
    if title is not None:
        title = ' '.join(str(title).split())[:150]
        if not title:
            raise ValueError('The title can\'t be empty.')
        if Product.objects.filter(vendor=vendor, title__iexact=title).exclude(pk=product.pk).exists():
            raise ValueError('You already have a product with this title.')
        product.title = title
        fields += ['title', 'slug']
    if description is not None:
        product.description = clean_html(description)
        fields.append('description')
    if features is not None:
        product.features = clean_html(features)
        fields.append('features')
    if fields:
        product.save()            # full save: slug, search vector and cache invalidation follow
        cache.delete_many([product_detail_cache_key(product), _scores_key(vendor)])
    return product

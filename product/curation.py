"""
product/curation.py

The automatic merchandiser behind Occasions and Collections.

Runs nightly (product.tasks.curate_collections, 04:00 UTC — after the
recommender retrain and with fresh deal scores) and does three jobs:

  1. Fill auto collections (filter_type='auto'). Each collection is a recipe —
     where to look, a strategy, filters, a size — and the curator scores every
     eligible product against it and saves a ranked list of ids.
  2. Pick themes for occasions with auto_sections=True: which sub-categories
     (or categories) deserve a card right now, and what to call them
     ("Skin care up to 30% off", "Trending in Headphones").
  3. Colour the cards from a palette that keeps white text readable.

Rotation is staggered: each collection or generated slot has its own
next_rotation_at, offset so the cards of one occasion don't all change on the
same night. Between rotations the nightly run only repairs — products that
sold out, lost their discount or were unpublished are swapped for the next best
— so a shopper never meets a dead tile, and nothing reshuffles needlessly.

Within an occasion a product appears in at most one card. Sections that are
not due keep their products and claim them first; due sections pick from the
rest.

Holidays: SeasonalEvent rows (admin → "Holiday calendar") open their own
occasion each year — fixed dates, nth-weekday rules, Easter, or listed dates
for moving holidays. Countries: an occasion with countries is shown only to
shoppers there (product/regions.py) and only picks products that ship to all
of them; the storefront also hides products that don't ship to the shopper.

Extending: a new ranking strategy is a STRATEGY_WEIGHTS entry (plus its
model choice); a new kind of card theme is a function in THEME_SOURCES.

Manual: `python manage.py curate_collections [--force] [--occasion SLUG]`.
"""

import logging
import math
import random
import re
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.html import strip_tags

from .models import Category, Collection, Occasion, OccasionSection, Product, SeasonalEvent, Sub_Category

logger = logging.getLogger(__name__)

# Card colours for a white page. Every one keeps white text at WCAG AA
# (contrast >= 4.5:1, asserted in tests) and none is so dark it reads as black.
PALETTE = [
    '#0071CE',  # blue
    '#C2410C',  # orange
    '#047857',  # emerald
    '#7C3AED',  # violet
    '#BE123C',  # rose
    '#0F766E',  # teal
    '#4338CA',  # indigo
    '#B45309',  # amber
    '#A21CAF',  # fuchsia
    '#1D4ED8',  # royal blue
]

# Signal weights per strategy: deal, popularity, rating, freshness.
STRATEGY_WEIGHTS = {
    'balanced':  (0.30, 0.35, 0.20, 0.15),
    'deals':     (0.60, 0.20, 0.15, 0.05),
    'trending':  (0.15, 0.60, 0.15, 0.10),
    'top_rated': (0.10, 0.20, 0.60, 0.10),
    'new':       (0.10, 0.15, 0.10, 0.65),
}

RECENTLY_SHOWN_PENALTY = 0.6   # score multiplier for products in the previous rotation
SAME_SUBCATEGORY_DECAY = 0.85  # each repeat of a sub-category in one card costs 15%
JITTER = 0.10                  # ±10% seeded noise so equal-ish products take turns
MIN_THEME_PRODUCTS = 8         # a generated theme needs this many products to earn a card
MAX_THEMES_PER_CATEGORY = 2    # keeps a row from being six flavours of one department
RECENT_THEME_PENALTY = 0.7
OCCASIONS_CACHE_VERSION_KEY = 'occasions_list_version'


def contrast_with_white(hex_color: str) -> float:
    """WCAG contrast ratio of white text on `hex_color`."""
    n = int(hex_color.lstrip('#'), 16)

    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (n >> 16) & 255, (n >> 8) & 255, n & 255
    luminance = 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    return 1.05 / (luminance + 0.05)


# ── Candidate pool ───────────────────────────────────────────────────────────

@dataclass
class Candidate:
    id: int
    vendor_id: int
    brand_id: int | None
    sub_category_id: int | None
    category_id: int | None
    price: float
    discount: float          # percent; anti-inflation figure from ProductDealScore when there is one
    deal: float              # learned deal score, or the discount when unscored
    trending: float
    rating: float            # Bayesian-smoothed, 0–5
    age_days: float
    text: str
    regions: frozenset = frozenset()  # Country ids it ships to; empty = everywhere


def _stem(word):
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 4 and word.endswith('es') and word[-3] in 'sxz':
        return word[:-2]
    if len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
        return word[:-1]
    return word


def _analyze(text):
    return [_stem(w) for w in re.findall(r'[a-z0-9]+', text.lower()) if len(w) > 1]


class CandidatePool:
    """
    Every product the curator may show: published, in stock, with a photo.
    Loaded once per run and shared by every occasion and collection, and the
    keyword index is built over it once, so adding occasions (holidays,
    countries) costs a filter pass each rather than another catalogue scan.
    """

    def __init__(self, candidates):
        self.candidates = candidates
        self.by_id = {c.id: c for c in candidates}
        self._row = {c.id: i for i, c in enumerate(candidates)}
        self._index = None

    def relevance(self, candidates, keywords):
        """TF-IDF cosine between the keywords and each candidate, scaled to 0–1. None without keywords."""
        terms = [k.strip() for k in (keywords or '').split(',') if k.strip()]
        if not terms or not candidates:
            return None
        if self._index is None:
            from sklearn.feature_extraction.text import TfidfVectorizer
            vectorizer = TfidfVectorizer(analyzer=_analyze, sublinear_tf=True)
            try:
                matrix = vectorizer.fit_transform([c.text for c in self.candidates])
            except ValueError:  # empty vocabulary
                vectorizer = matrix = None
            self._index = (vectorizer, matrix)
        vectorizer, matrix = self._index
        if vectorizer is None:
            return np.zeros(len(candidates))
        query = vectorizer.transform([' '.join(terms)])
        rows = [self._row[c.id] for c in candidates]
        sims = (matrix[rows] @ query.T).toarray().ravel()
        top = sims.max() if sims.size else 0
        return sims / top if top > 0 else sims

    @classmethod
    def load(cls, now=None):
        from recommendation.deals import _stock_levels
        from recommendation.models import ProductDealScore

        now = now or timezone.now()
        published = Product.published.exclude(Q(image='') | Q(image__isnull=True)).filter(price__gt=0)
        rows = list(
            published.values(
                'id', 'vendor_id', 'brand_id', 'sub_category_id', 'sub_category__category_id',
                'price', 'old_price', 'trending_score', 'avg_rating', 'review_count', 'date',
                'title', 'description', 'sub_category__title', 'sub_category__category__title', 'brand__title',
            ).iterator(chunk_size=2000)
        )
        ids = [r['id'] for r in rows]
        stock = _stock_levels(ids) if ids else {}
        regions = {}
        for product_id, country_id in Product.available_in_regions.through.objects.filter(
                product__in=published).values_list('product_id', 'country_id').iterator(chunk_size=5000):
            regions.setdefault(product_id, set()).add(country_id)
        deals = {
            d['product_id']: d for d in ProductDealScore.objects.filter(product_id__in=ids).values(
                'product_id', 'discount_percent', 'score', 'best_price',
            )
        }

        candidates = []
        for r in rows:
            if stock.get(r['id'], 0) <= 0:
                continue
            price = float(r['price'])
            old = float(r['old_price'] or 0)
            scored = deals.get(r['id'])
            if scored:
                discount = float(scored['discount_percent'] or 0)
                deal = float(scored['score'] or 0)
                if scored['best_price']:
                    price = float(scored['best_price'])
            else:
                discount = (old - price) / old * 100 if old > price > 0 else 0.0
                deal = discount / 100
            # Bayesian average: a 5.0 from two reviews shouldn't beat a 4.6 from two hundred.
            rating = (r['avg_rating'] * r['review_count'] + 3.5 * 5) / (r['review_count'] + 5)
            text = ' '.join(filter(None, [
                r['title'], r['brand__title'], r['sub_category__title'], r['sub_category__category__title'],
                strip_tags(r['description'] or '')[:300],
            ]))
            candidates.append(Candidate(
                id=r['id'], vendor_id=r['vendor_id'], brand_id=r['brand_id'],
                sub_category_id=r['sub_category_id'], category_id=r['sub_category__category_id'],
                price=price, discount=max(0.0, discount), deal=deal, trending=float(r['trending_score'] or 0),
                rating=rating, age_days=max(0.0, (now - r['date']).total_seconds() / 86400) if r['date'] else 365.0,
                text=text, regions=frozenset(regions.get(r['id'], ())),
            ))
        return cls(candidates)


def _percentiles(values):
    arr = np.asarray(values, dtype=float)
    if arr.size <= 1:
        return np.ones_like(arr)
    from scipy.stats import rankdata
    # Ties share a rank: twenty unscored products must not get twenty different scores.
    return (rankdata(arr, method='average') - 1) / (arr.size - 1)


def score_candidates(pool, candidates, strategy='balanced', keywords='', recent_ids=(), seed=None):
    """→ {product_id: score}. Products that miss every keyword are dropped when keywords are given."""
    if not candidates:
        return {}
    w_deal, w_pop, w_rating, w_fresh = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS['balanced'])
    deal = _percentiles([c.deal for c in candidates])
    pop = _percentiles([c.trending for c in candidates])
    rating = np.array([(c.rating - 1) / 4 for c in candidates])
    fresh = np.array([math.exp(-c.age_days / 30) for c in candidates])
    base = w_deal * deal + w_pop * pop + w_rating * rating + w_fresh * fresh

    relevance = pool.relevance(candidates, keywords)
    rng = random.Random(seed)
    recent = set(recent_ids)
    scores = {}
    for i, c in enumerate(candidates):
        s = float(base[i])
        if relevance is not None:
            if relevance[i] <= 0:
                continue
            s *= 0.4 + 0.6 * float(relevance[i])
        if c.id in recent:
            s *= RECENTLY_SHOWN_PENALTY
        s *= 1 + rng.uniform(-JITTER, JITTER)
        scores[c.id] = s
    return scores


def select_products(pool, scores, size, max_per_vendor, pinned=(), taken=()):
    """
    Greedy pick: pinned first, then best score with a hard vendor cap and a soft
    sub-category spread. If caps leave the card short, a second pass relaxes the
    vendor cap rather than show empty tiles.
    """
    taken = set(taken)
    chosen = [pid for pid in pinned if pid in pool.by_id][:size]
    chosen_set = set(chosen)
    vendors, subcats = {}, {}
    for pid in chosen:
        c = pool.by_id[pid]
        vendors[c.vendor_id] = vendors.get(c.vendor_id, 0) + 1
        subcats[c.sub_category_id] = subcats.get(c.sub_category_id, 0) + 1

    ranked = sorted((pid for pid in scores if pid not in taken and pid not in chosen_set),
                    key=scores.get, reverse=True)[:max(size * 15, 200)]

    def fill(respect_vendor_cap):
        remaining = [pid for pid in ranked if pid not in chosen_set]
        while len(chosen) < size and remaining:
            best, best_score = None, -1.0
            for pid in remaining:
                c = pool.by_id[pid]
                if respect_vendor_cap and vendors.get(c.vendor_id, 0) >= max_per_vendor:
                    continue
                adjusted = scores[pid] * SAME_SUBCATEGORY_DECAY ** subcats.get(c.sub_category_id, 0)
                if adjusted > best_score:
                    best, best_score = pid, adjusted
            if best is None:
                return
            remaining.remove(best)
            c = pool.by_id[best]
            chosen.append(best)
            chosen_set.add(best)
            vendors[c.vendor_id] = vendors.get(c.vendor_id, 0) + 1
            subcats[c.sub_category_id] = subcats.get(c.sub_category_id, 0) + 1

    fill(respect_vendor_cap=True)
    fill(respect_vendor_cap=False)
    return chosen


# ── Collections ──────────────────────────────────────────────────────────────

def ships_to(candidate, country_ids):
    """True when the product can be sold in every one of `country_ids` (none given = no restriction)."""
    return not country_ids or not candidate.regions or set(country_ids) <= candidate.regions


def collection_candidates(collection, pool, countries=()):
    """The pool narrowed by the collection's recipe (scope, price, discount, exclusions, countries)."""
    cat_ids = set(collection.auto_categories.values_list('id', flat=True))
    sub_ids = set(collection.auto_sub_categories.values_list('id', flat=True))
    excluded = set(collection.excluded_products.values_list('id', flat=True))
    min_price = float(collection.auto_min_price) if collection.auto_min_price is not None else None
    max_price = float(collection.auto_max_price) if collection.auto_max_price is not None else None

    out = []
    for c in pool.candidates:
        if c.id in excluded or not ships_to(c, countries):
            continue
        if (cat_ids or sub_ids) and c.category_id not in cat_ids and c.sub_category_id not in sub_ids:
            continue
        if collection.auto_min_discount and c.discount < collection.auto_min_discount:
            continue
        if min_price is not None and c.price < min_price:
            continue
        if max_price is not None and c.price > max_price:
            continue
        out.append(c)
    return out


def is_due(collection, now):
    return not collection.curated_product_ids or not collection.next_rotation_at or collection.next_rotation_at <= now


def _next_rotation(now, days, offset_days=0):
    # An hour early so tomorrow's 04:00 run finds it due rather than a few seconds short.
    return now + timedelta(days=days + offset_days) - timedelta(hours=1)


def curate_collection(collection, pool, now, taken=(), force=False, stagger=0, countries=()):
    """
    Rotate (reshuffle) when due, otherwise repair: keep the current order, drop
    products that are no longer eligible and top up. Returns the chosen ids.
    `countries` comes from the occasion the collection sits in.
    """
    eligible = collection_candidates(collection, pool, countries)
    eligible_ids = {c.id for c in eligible}
    pinned = [pid for pid in collection.pinned_products.values_list('id', flat=True) if pid in pool.by_id]
    taken = set(taken)
    rotating = force or is_due(collection, now)

    if rotating:
        recent = collection.curated_product_ids or []
        scores = score_candidates(pool, eligible, collection.auto_strategy, collection.auto_keywords,
                                  recent_ids=recent, seed=f'{collection.pk}:{now.date()}')
        chosen = select_products(pool, scores, collection.auto_size, collection.max_per_vendor,
                                 pinned=pinned, taken=taken)
        collection.recent_product_ids = recent
        first_time = not collection.last_curated_at
        collection.next_rotation_at = _next_rotation(
            now, collection.rotation_days, offset_days=stagger if first_time else 0,
        )
    else:
        kept = [pid for pid in collection.curated_product_ids
                if pid in eligible_ids and pid not in taken and pid not in pinned]
        if len(kept) == len(collection.curated_product_ids) and not set(pinned) - set(kept):
            return list(collection.curated_product_ids)  # nothing to repair
        scores = score_candidates(pool, eligible, collection.auto_strategy, collection.auto_keywords,
                                  recent_ids=collection.recent_product_ids, seed=f'{collection.pk}:{now.date()}')
        # Kept products stay in place; the top-up fills the end.
        scores.update({pid: 1e6 - i for i, pid in enumerate(kept)})
        chosen = select_products(pool, scores, collection.auto_size, collection.max_per_vendor,
                                 pinned=pinned, taken=taken)

    if len(chosen) < 4:
        logger.warning('Collection %s: only %d eligible products — check its recipe', collection.slug, len(chosen))
    collection.curated_product_ids = chosen
    collection.last_curated_at = now
    collection.save(update_fields=['curated_product_ids', 'recent_product_ids', 'last_curated_at',
                                   'next_rotation_at', 'updated_at'])
    return chosen


# ── Occasion themes ──────────────────────────────────────────────────────────

@dataclass
class Theme:
    key: str                  # 'sub:12' or 'cat:4'
    name: str
    category_id: int | None
    strategy: str
    min_discount: int
    strength: float
    product_ids: list = field(default_factory=list)


def _theme_strategy(members):
    on_sale = [c for c in members if c.discount >= 10]
    if len(on_sale) >= 6:
        return 'deals', 10
    if sum(1 for c in members if c.trending > 0) >= MIN_THEME_PRODUCTS:
        return 'trending', 0
    reviewed = [c for c in members if c.rating >= 4.0]
    if len(reviewed) >= 4:
        return 'top_rated', 0
    return 'balanced', 0


def occasion_scope(occasion, pool, now, countries=()):
    """The occasion's slice of the pool (its categories, keywords, countries) with balanced scores."""
    cat_ids = set(occasion.auto_categories.values_list('id', flat=True))
    scope = [c for c in pool.candidates
             if (not cat_ids or c.category_id in cat_ids) and ships_to(c, countries)]
    scores = score_candidates(pool, scope, 'balanced', occasion.auto_keywords, seed=f'occ{occasion.pk}:{now.date()}')
    return [c for c in scope if c.id in scores], scores


def category_themes(occasion, scope, scores):
    """Every sub-category (falling back to category) with enough products to fill a card."""
    by_sub, by_cat = {}, {}
    for c in scope:
        if c.sub_category_id:
            by_sub.setdefault(c.sub_category_id, []).append(c)
        if c.category_id:
            by_cat.setdefault(c.category_id, []).append(c)

    subs = {row[0]: row[1:] for row in Sub_Category.objects.filter(id__in=by_sub)
            .values_list('id', 'title', 'category_id', 'engagement_score')}
    cat_names = dict(Category.objects.filter(id__in=by_cat).values_list('id', 'title'))

    groups = [(f'sub:{sid}', subs[sid][0], subs[sid][1], members)
              for sid, members in by_sub.items() if sid in subs and len(members) >= MIN_THEME_PRODUCTS]
    # A category becomes a theme only when none of its sub-categories is big enough alone.
    covered = {parent for key, _, parent, _ in groups}
    groups += [(f'cat:{cid}', cat_names.get(cid, ''), cid, members)
               for cid, members in by_cat.items() if len(members) >= MIN_THEME_PRODUCTS and cid not in covered]

    eng_ids = list(subs)
    eng_pct = dict(zip(eng_ids, _percentiles([subs[i][2] for i in eng_ids]))) if eng_ids else {}
    themes = []
    for key, name, category_id, members in groups:
        if not name:
            continue
        top = sorted((scores[c.id] for c in members), reverse=True)[:MIN_THEME_PRODUCTS]
        strength = float(np.mean(top))
        if key.startswith('sub:'):
            strength += 0.3 * float(eng_pct.get(int(key[4:]), 0))
        strategy, min_discount = _theme_strategy(members)
        themes.append(Theme(key, name, category_id, strategy, min_discount, strength))
    return themes


# Where card themes come from. Each source takes (occasion, scope, scores) and
# returns Themes; pick_themes ranks them all together. A new kind of card (say
# brand spotlights) is one more function here, plus its key prefix handled in
# _apply_theme / _theme_from_collection.
THEME_SOURCES = [category_themes]


def candidate_themes(occasion, pool, now, countries=()):
    scope, scores = occasion_scope(occasion, pool, now, countries)
    if not scope:
        return []
    return [theme for source in THEME_SOURCES for theme in source(occasion, scope, scores)]


def pick_themes(themes, slots, held=(), recent=(), max_per_category=MAX_THEMES_PER_CATEGORY):
    """Best themes for `slots` cards, skipping ones other cards already show and spreading departments."""
    held = list(held)
    held_keys = {t.key for t in held}
    per_category = {}
    per_strategy = {}
    for t in held:
        per_category[t.category_id] = per_category.get(t.category_id, 0) + 1
        per_strategy[t.strategy] = per_strategy.get(t.strategy, 0) + 1
    total = len(held) + slots
    recent = set(recent)

    def strength(t):
        return t.strength * (RECENT_THEME_PENALTY if t.key in recent else 1.0)

    picked = []
    for relax in (False, True):
        for t in sorted(themes, key=strength, reverse=True):
            if len(picked) == slots:
                return picked
            if t.key in held_keys or t.key in {p.key for p in picked}:
                continue
            if not relax and per_category.get(t.category_id, 0) >= max_per_category:
                continue
            strategy = t.strategy
            # Keep the row varied: deals headline at most half the cards.
            if strategy == 'deals' and per_strategy.get('deals', 0) >= math.ceil(total / 2):
                t = Theme(t.key, t.name, t.category_id, 'trending', 0, t.strength)
            picked.append(t)
            per_category[t.category_id] = per_category.get(t.category_id, 0) + 1
            per_strategy[t.strategy] = per_strategy.get(t.strategy, 0) + 1
    return picked


def theme_title(theme, collection, pool):
    if theme.strategy == 'deals':
        best = max((pool.by_id[pid].discount for pid in collection.curated_product_ids if pid in pool.by_id),
                   default=0)
        rounded = int(best // 5 * 5)  # rounded down so "up to" is always true
        return f'{theme.name} up to {rounded}% off' if rounded >= 20 else f'Deals on {theme.name}'
    if theme.strategy == 'trending':
        return f'Trending in {theme.name}'
    if theme.strategy == 'top_rated':
        return f'Top-rated {theme.name}'
    return f'Shop {theme.name}'


def _theme_from_collection(collection):
    """Rebuild the Theme a generated collection was made from, out of its scope and strategy."""
    sub = collection.auto_sub_categories.first()
    if sub:
        return Theme(f'sub:{sub.id}', sub.title, sub.category_id, collection.auto_strategy,
                     collection.auto_min_discount, 0.0)
    cat = collection.auto_categories.first()
    if cat:
        return Theme(f'cat:{cat.id}', cat.title, cat.id, collection.auto_strategy,
                     collection.auto_min_discount, 0.0)
    return None


def _apply_theme(collection, theme, occasion):
    collection.filter_type = 'auto'
    collection.title = theme.name
    collection.auto_strategy = theme.strategy
    collection.auto_min_discount = theme.min_discount
    collection.auto_keywords = occasion.auto_keywords
    collection.rotation_days = occasion.rotation_days
    collection.is_active = True
    collection.save()
    kind, pk = theme.key.split(':')
    collection.auto_sub_categories.set([int(pk)] if kind == 'sub' else [])
    collection.auto_categories.set([int(pk)] if kind == 'cat' else [])


def sync_generated_sections(occasion, pool, now, force=False, countries=()):
    """
    Keep `auto_section_count` cards on the occasion (counting hand-made ones).
    Each generated slot changes theme on its own staggered day; a slot whose
    theme can no longer fill a card is replaced at once.
    """
    sections = list(occasion.sections.select_related('collection').order_by('position'))
    manual = [s for s in sections if not s.is_generated]
    generated = [s for s in sections if s.is_generated]
    wanted = max(0, occasion.auto_section_count - len(manual))

    for extra in generated[wanted:]:
        if extra.collection_id and extra.collection.is_generated:
            extra.collection.delete()
        extra.delete()
    generated = generated[:wanted]

    themes = candidate_themes(occasion, pool, now, countries)
    viable = {t.key: t for t in themes}
    current = {}
    for s in generated:
        t = _theme_from_collection(s.collection) if s.collection_id else None
        if t and t.key in viable:
            current[s.pk] = t

    def locked(s):
        return s.collection_id and s.collection.is_locked

    due = [s for s in generated
           if not locked(s) and (force or s.pk not in current or is_due(s.collection, now))]
    held = [current[s.pk] for s in generated if s not in due and s.pk in current]
    slots_to_add = wanted - len(generated)
    fresh = pick_themes(themes, len(due) + slots_to_add, held=held, recent=occasion.recent_theme_ids)

    def slot_collection(index):
        # Slugs are the "View all" URLs, so keep them short and stable; skip any already in use.
        n = index + 1
        while Collection.objects.filter(slug=f'{occasion.slug}-picks-{n}').exists():
            n += 1
        return Collection.objects.create(slug=f'{occasion.slug}-picks-{n}', title=occasion.title,
                                         filter_type='auto', is_generated=True)

    base_position = max((s.position for s in manual), default=-1) + 1
    annotated = {}
    retired = []
    for section, theme in zip(due, fresh):
        old = current.get(section.pk)
        if old and old.key != theme.key:
            retired.append(old.key)
        if not section.collection_id:  # its collection was deleted by hand
            section.collection = slot_collection(generated.index(section))
            section.save(update_fields=['collection'])
        _apply_theme(section.collection, theme, occasion)
        section.collection.next_rotation_at = None  # rotate now
        section.collection._theme = theme
        annotated[section.collection.pk] = section.collection
    for i, theme in enumerate(fresh[len(due):]):
        index = len(generated) + i
        collection = slot_collection(index)
        _apply_theme(collection, theme, occasion)
        collection._theme = theme
        # Stagger: slot i first rotates i days after slot 0 (mod rotation_days).
        collection._stagger = index % max(1, occasion.rotation_days)
        annotated[collection.pk] = collection
        OccasionSection.objects.create(occasion=occasion, title=theme.name, collection=collection,
                                       position=base_position + index, is_generated=True)
    for section in due[len(fresh):]:  # no theme left for this slot
        if section.collection_id and section.collection.is_generated:
            section.collection.delete()
        section.delete()

    if retired:
        occasion.recent_theme_ids = (retired + list(occasion.recent_theme_ids))[:occasion.auto_section_count * 2]
        occasion.save(update_fields=['recent_theme_ids'])

    # Callers reload sections; hand back the in-memory theme/stagger annotations.
    return annotated


# ── Colours ──────────────────────────────────────────────────────────────────

def paint(occasion, sections, now):
    if occasion.color_mode == 'fixed':
        return
    due = (not occasion.last_rotated_at
           or occasion.last_rotated_at + timedelta(days=occasion.rotation_days) - timedelta(hours=1) <= now)
    if due:
        try:
            start = (PALETTE.index(occasion.accent_color.upper()) + 3) % len(PALETTE)
        except ValueError:
            start = occasion.pk % len(PALETTE)
        occasion.accent_color = PALETTE[start]
        occasion.last_rotated_at = now
        occasion.save(update_fields=['accent_color', 'last_rotated_at'])
    start = PALETTE.index(occasion.accent_color.upper()) if occasion.accent_color.upper() in PALETTE else 0
    for i, section in enumerate(sections):
        color = PALETTE[(start + i) % len(PALETTE)] if occasion.color_mode == 'mixed' else ''
        if section.accent_color != color:
            section.accent_color = color
            section.save(update_fields=['accent_color'])
        collection = section.collection
        if collection and collection.filter_type == 'auto':
            card = color or occasion.accent_color
            if collection.accent_color != card:
                collection.accent_color = card
                collection.save(update_fields=['accent_color'])


# ── Orchestration ────────────────────────────────────────────────────────────

def _active_occasions(today):
    # Includes occasions starting tomorrow, so they open already stocked.
    return Occasion.objects.filter(is_active=True).filter(
        Q(start_date__isnull=True) | Q(start_date__lte=today + timedelta(days=1))
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today))


def open_seasonal_occasions(today):
    """
    Open an Occasion for every enabled holiday whose window covers today (or
    starts tomorrow). One occasion per holiday per year, slug '<event>-<year>';
    its dates follow the calendar, everything else is the admin's to change.
    """
    opened = []
    for event in SeasonalEvent.objects.filter(is_enabled=True).prefetch_related('countries', 'categories'):
        window = event.window_for(today)
        if not window:
            continue
        start, holiday, end = window
        occasion, created = Occasion.objects.get_or_create(
            slug=f'{event.slug}-{holiday.year}',
            defaults=dict(
                title=event.title, subtitle=event.subtitle, icon=event.icon, event=event,
                start_date=start, end_date=end, position=event.position, accent_color=event.accent_color,
                color_mode=event.color_mode, auto_sections=True, auto_section_count=event.section_count,
                auto_keywords=event.keywords, rotation_days=event.rotation_days,
            ),
        )
        if created:
            occasion.countries.set(event.countries.all())
            occasion.auto_categories.set(event.categories.all())
            opened.append(occasion.slug)
        elif (occasion.start_date, occasion.end_date) != (start, end):  # a listed date was corrected
            occasion.start_date, occasion.end_date = start, end
            occasion.save(update_fields=['start_date', 'end_date'])
    return opened


def curate_occasion(occasion, pool, now, force=False, done=None):
    done = set() if done is None else done
    countries = set(occasion.countries.values_list('id', flat=True))
    with transaction.atomic():
        annotated = (sync_generated_sections(occasion, pool, now, force, countries)
                     if occasion.auto_sections else {})
        sections = list(occasion.sections.select_related('collection').order_by('position'))

        taken = set()
        auto, others = [], []
        for s in sections:
            c = s.collection
            if c and c.filter_type == 'auto' and not c.is_locked and c.is_active:
                auto.append(s)
            elif c:
                others.append(s)
        for s in others:
            taken.update(s.collection.get_products_qs().values_list('id', flat=True)[:50])
        # Cards that aren't changing tonight keep their products; due ones pick from the rest.
        auto.sort(key=lambda s: force or is_due(annotated.get(s.collection_id, s.collection), now))
        for s in auto:
            collection = annotated.get(s.collection_id, s.collection)
            if collection.pk in done:
                taken.update(collection.curated_product_ids)
                continue
            stagger = getattr(collection, '_stagger', s.position % max(1, collection.rotation_days))
            chosen = curate_collection(collection, pool, now, taken=taken, force=force, stagger=stagger,
                                       countries=countries)
            taken.update(chosen)
            done.add(collection.pk)
            # Retitle every night: a repair can change the deepest discount behind "up to X% off".
            theme = getattr(collection, '_theme', None) or (
                _theme_from_collection(collection) if collection.is_generated else None)
            if theme and (collection.title != theme_title(theme, collection, pool) or s.title != collection.title):
                title = theme_title(theme, collection, pool)
                collection.title = title
                collection.save(update_fields=['title'])
                s.title = title
                s.save(update_fields=['title'])
        paint(occasion, sections, now)
    return done


def bump_occasions_cache():
    try:
        cache.incr(OCCASIONS_CACHE_VERSION_KEY)
    except ValueError:
        cache.set(OCCASIONS_CACHE_VERSION_KEY, 1, None)


def run(now=None, force=False, occasion_slug=None):
    """Curate every active occasion and standalone auto collection. Returns a summary dict."""
    now = now or timezone.now()
    opened = [] if occasion_slug else open_seasonal_occasions(now.date())
    pool = CandidatePool.load(now)
    done = set()
    occasions = _active_occasions(now.date())
    if occasion_slug:
        occasions = Occasion.objects.filter(slug=occasion_slug)
    for occasion in occasions:
        try:
            done = curate_occasion(occasion, pool, now, force=force, done=done)
        except Exception:
            logger.exception('Curating occasion %s failed', occasion.slug)

    standalone = 0
    if not occasion_slug:
        for collection in Collection.objects.filter(filter_type='auto', is_active=True, is_locked=False) \
                .exclude(pk__in=done):
            try:
                curate_collection(collection, pool, now, force=force,
                                  stagger=collection.pk % max(1, collection.rotation_days))
                standalone += 1
            except Exception:
                logger.exception('Curating collection %s failed', collection.slug)

    bump_occasions_cache()
    summary = {'pool': len(pool.candidates), 'opened': opened, 'occasion_collections': len(done),
               'standalone': standalone}
    logger.info('Curation finished: %s', summary)
    return summary

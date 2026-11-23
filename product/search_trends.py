"""
product/search_trends.py

What shoppers search for, for the search bar's "Trending" chips and the
"popular searches" suggested while typing.

- record_search(): called by the search results endpoint. Counts each visitor
  at most once per term per day, and only searches that found products, so a
  single shopper hammering Enter (or a typo nobody else makes) can't trend.
- trending_searches(): last 7 days, newer days weigh more. A term must have
  been searched by MIN_SEARCHERS different shoppers before it is shown to
  anyone, which also keeps one person's private query (a name, a phone number)
  from being shown to everyone else. Topped up with popular category names
  while the site has little search traffic.
- complete_query(): popular terms matching what is being typed.

Admins hide a term (and anything containing it) with BlockedSearchTerm.
"""
import hashlib
import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.db.models import F, Value
from django.db.models.functions import Coalesce, Least
from django.utils import timezone

from .models import BlockedSearchTerm, Product, SearchTermDaily
from .utils import _get_visitor_id, detect_bot

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7
DAILY_DECAY = 0.85          # yesterday counts 85% of today, and so on
MIN_SEARCHERS = 3           # different shoppers before a term is ever shown
MAX_TERM_LEN = 80
MAX_WORDS = 8
KEEP_DAYS = 60              # rows older than this are deleted nightly

TRENDING_CACHE_KEY = 'search_trends:trending:v1'
POOL_CACHE_KEY = 'search_trends:pool:v1'
CACHE_TTL = 10 * 60

_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_EMAIL = re.compile(r"\S+@\S+")
_URL = re.compile(r"(https?://|www\.)", re.I)
_LONG_NUMBER = re.compile(r"\d[\d\s\-]{6,}\d")   # phone numbers, card numbers…


def normalize_term(raw) -> str | None:
    """Lowercase, single-spaced, trimmed; None when it shouldn't be counted."""
    if not isinstance(raw, str):
        return None
    term = ' '.join(raw.lower().split())
    term = _EDGE_PUNCT.sub('', term)
    if len(term) < 2 or len(term) > MAX_TERM_LEN:
        return None
    if len(term.split()) > MAX_WORDS or not _HAS_LETTER.search(term):
        return None
    if _EMAIL.search(term) or _URL.search(term) or _LONG_NUMBER.search(term):
        return None
    return term


def record_search(request, query, results: int) -> None:
    """
    Count one shopper's search for `query` (`results` = products it found,
    filters aside). Searches that found something feed trending; ones that
    found nothing are counted separately, for the sellers' demand insights.
    Never raises.
    """
    try:
        if detect_bot(request):
            return
        term = normalize_term(query)
        if not term:
            return
        visitor = _get_visitor_id(request)
        if not visitor:
            return
        today = timezone.localdate()
        digest = hashlib.md5(f"{visitor}|{term}".encode()).hexdigest()
        if not cache.add(f"search_trends:seen:{today:%Y%m%d}:{digest}", 1, 26 * 3600):
            return   # this shopper already counted for this term today
        found = bool(results and results > 0)
        if found:
            changes = {'searchers': F('searchers') + 1,
                       'fewest_results': Least(Coalesce('fewest_results', Value(results)), Value(results))}
        else:
            changes = {'no_result_searchers': F('no_result_searchers') + 1}
        if not SearchTermDaily.objects.filter(term=term, date=today).update(**changes):
            obj, created = SearchTermDaily.objects.get_or_create(
                term=term, date=today,
                defaults={'searchers': 1, 'fewest_results': results} if found else {'no_result_searchers': 1})
            if not created:
                SearchTermDaily.objects.filter(pk=obj.pk).update(**changes)
    except Exception:
        logger.warning("record_search failed for %r", query, exc_info=True)


def _blocked() -> list[str]:
    return list(BlockedSearchTerm.objects.values_list('term', flat=True))


def _is_blocked(term: str, blocked: list[str]) -> bool:
    return any(b and b in term for b in blocked)


def _ranked_terms(limit: int) -> list[str]:
    """Terms from the last WINDOW_DAYS, best first, already filtered."""
    today = timezone.localdate()
    since = today - timedelta(days=WINDOW_DAYS - 1)
    rows = (SearchTermDaily.objects.filter(date__gte=since, searchers__gt=0)
            .order_by('-searchers').values_list('term', 'date', 'searchers')[:5000])
    score: dict[str, float] = {}
    total: dict[str, int] = {}
    for term, day, n in rows:
        age = max(0, (today - day).days)
        score[term] = score.get(term, 0.0) + n * (DAILY_DECAY ** age)
        total[term] = total.get(term, 0) + n
    blocked = _blocked()
    ranked = sorted((t for t in score if total[t] >= MIN_SEARCHERS and not _is_blocked(t, blocked)),
                    key=lambda t: (-score[t], t))
    return ranked[:limit]


def _category_fallback(limit: int, exclude: set[str]) -> list[str]:
    """Names of the categories of today's trending products."""
    out: list[str] = []
    blocked = _blocked()
    rows = (Product.published.filter(sub_category__isnull=False)
            .order_by('-trending_score', '-views').values_list('sub_category__title', flat=True)[:300])
    for title in rows:
        term = normalize_term(title or '')
        if term and term not in exclude and term not in out and not _is_blocked(term, blocked):
            out.append(term)
            if len(out) >= limit:
                break
    return out


def trending_searches(limit: int = 12) -> list[str]:
    cached = cache.get(TRENDING_CACHE_KEY)
    if cached is None:
        terms = _ranked_terms(20)
        if len(terms) < 20:
            terms += _category_fallback(20 - len(terms), set(terms))
        cached = terms
        cache.set(TRENDING_CACHE_KEY, cached, CACHE_TTL)
    return cached[:limit]


def _popular_pool() -> list[str]:
    pool = cache.get(POOL_CACHE_KEY)
    if pool is None:
        pool = _ranked_terms(1000)
        cache.set(POOL_CACHE_KEY, pool, CACHE_TTL)
    return pool


def complete_query(query, limit: int = 5) -> list[str]:
    """Popular searches that start with (or have a word starting with) `query`."""
    q = ' '.join((query or '').lower().split())
    if len(q) < 2:
        return []
    starts, inner = [], []
    for term in _popular_pool():
        if term == q:
            continue
        if term.startswith(q):
            starts.append(term)
        elif f' {q}' in f' {term}':
            inner.append(term)
        if len(starts) >= limit:
            break
    return (starts + inner)[:limit]


def purge_old_terms() -> int:
    cutoff = timezone.localdate() - timedelta(days=KEEP_DAYS)
    deleted, _ = SearchTermDaily.objects.filter(date__lt=cutoff).delete()
    return deleted

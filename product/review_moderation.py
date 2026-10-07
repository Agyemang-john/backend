"""
product/review_moderation.py
Automatic content checks for reviews. Decides APPROVED vs PENDING only;
nothing is ever auto-rejected, and nothing here looks at the star rating or
whether the review is positive or negative.

A rule returns a flag (short machine-readable string) when it finds a
problem. No flags → the review publishes immediately. Any flag → it waits for
staff in the moderation queue, with the flags shown to the moderator.

Configuration (settings.REVIEW_MODERATION, all optional):

    REVIEW_MODERATION = {
        'BLOCKED_TERMS': ['...'],          # abusive / prohibited words (word match)
        'PROMO_TERMS': ['whatsapp me'],    # advertising phrases (substring match)
        'MAX_REVIEWS_PER_HOUR': 5,         # per customer, across products
        'MEDIA_REQUIRES_MODERATION': False,# hold every review with photos/video
        'AI_ADAPTER': None,                # dotted path, see AIModerationAdapter
    }

The AI adapter is optional and off by default, so there is no paid dependency.
To plug one in, implement AIModerationAdapter.check() and set AI_ADAPTER.
Adapter failures never block a review: they are logged and the rules decide.
"""

import hashlib
import logging
import re
import unicodedata
from datetime import timedelta
from functools import lru_cache

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

logger = logging.getLogger('reviews')

DEFAULT_PROMO_TERMS = (
    'whatsapp', 'telegram', 'call me', 'dm me', 'inbox me', 'promo code', 'discount code',
    'buy cheaper', 'cheaper at', 'visit my', 'follow me', 'click here', 'earn money',
)
URL_RE = re.compile(r'(https?://|www\.)\S+|\b[a-z0-9-]+\.(com|net|org|shop|store|link|ly|io|gh)\b', re.I)
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
# 9+ digits, allowing spaces/dashes/brackets and a leading +, e.g. +233 24 123 4567
PHONE_RE = re.compile(r'(?:\+?\d[\s\-().]*){9,}')
REPEATED_CHAR_RE = re.compile(r'(.)\1{7,}')


def _config():
    return getattr(settings, 'REVIEW_MODERATION', {})


def normalise(text):
    text = unicodedata.normalize('NFKC', text or '').lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', text)).strip()


def content_hash(title, text):
    return hashlib.sha256(normalise(f"{title} {text}").encode()).hexdigest()


# ── Rules ─────────────────────────────────────────────────────────────────────

def rule_contact_details(text, **_):
    flags = []
    if EMAIL_RE.search(text):
        flags.append('contains_email')
    if PHONE_RE.search(text):
        flags.append('contains_phone_number')
    return flags


def rule_links(text, **_):
    return ['contains_link'] if URL_RE.search(text) else []


def rule_promotional(text, **_):
    lowered = normalise(text)
    terms = _config().get('PROMO_TERMS', DEFAULT_PROMO_TERMS)
    return ['promotional_content'] if any(t in lowered for t in terms) else []


def rule_blocked_terms(text, **_):
    words = set(normalise(text).split())
    blocked = {t.lower() for t in _config().get('BLOCKED_TERMS', [])}
    return ['prohibited_language'] if words & blocked else []


def rule_spammy_text(text, **_):
    words = normalise(text).split()
    if REPEATED_CHAR_RE.search(text):
        return ['repetitive_characters']
    if len(words) >= 8 and len(set(words)) / len(words) < 0.3:
        return ['repetitive_words']
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 30 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return ['excessive_capitals']
    return []


def rule_duplicate_content(text, *, review, **_):
    """Same text as another review (copy-paste across products or accounts)."""
    from .models import ProductReview
    if len(normalise(text)) < 25:  # "Good product, thanks" is common and fine
        return []
    clash = ProductReview.objects.filter(content_hash=review.content_hash).exclude(pk=review.pk)
    return ['duplicate_text'] if clash.exists() else []


def rule_velocity(text, *, review, **_):
    """Unusually many reviews from one account in a short time."""
    from .models import ProductReview
    limit = _config().get('MAX_REVIEWS_PER_HOUR', 5)
    since = timezone.now() - timedelta(hours=1)
    recent = ProductReview.objects.filter(user_id=review.user_id, date__gte=since).exclude(pk=review.pk).count()
    return ['unusual_review_activity'] if recent >= limit else []


def rule_media_hold(text, *, has_media=False, **_):
    return ['media_requires_review'] if has_media and _config().get('MEDIA_REQUIRES_MODERATION') else []


RULES = [
    rule_contact_details, rule_links, rule_promotional, rule_blocked_terms,
    rule_spammy_text, rule_duplicate_content, rule_velocity, rule_media_hold,
]


# ── Optional AI adapter ───────────────────────────────────────────────────────

class AIModerationAdapter:
    """Interface for an optional external moderation service."""

    def check(self, text: str) -> list[str]:
        """Return flags (e.g. ['ai:harassment']) or [] when the text is acceptable."""
        raise NotImplementedError


@lru_cache(maxsize=1)
def _ai_adapter():
    path = _config().get('AI_ADAPTER')
    return import_string(path)() if path else None


def run_checks(review, *, has_media=False):
    """All flags for this review (empty list = publish automatically)."""
    text = f"{review.title}\n{review.review}"
    flags = []
    for rule in RULES:
        flags.extend(rule(text, review=review, has_media=has_media))
    adapter = _ai_adapter()
    if adapter is not None:
        try:
            flags.extend(adapter.check(text))
        except Exception:
            logger.exception("review moderation: AI adapter failed for review=%s", review.pk)
    return sorted(set(flags))

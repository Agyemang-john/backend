"""
recommendation/content.py

Content-based product embeddings: TF-IDF over each product's text and facets,
compressed with truncated SVD (latent semantic analysis) into a dense vector in
the same dimensionality as the collaborative factors.

This model is what carries the storefront early on. Collaborative filtering can
only recommend what somebody has already interacted with, which means it is
silent about every product listed this week and useless on day one. Content
similarity has no such gap: a product is embeddable the moment it has a title
and a category, so "You might also like this" works on a brand-new listing, and
new stock is never invisible just because it is new.

Two implementation notes:

* **Facets are injected as synthetic tokens** (`__subcat_12`, `__brand_5`,
  `__price_p4`) rather than bolted on as extra numeric columns. TF-IDF then
  weights them by rarity for free — a niche brand becomes a strong similarity
  signal while a catch-all category stays weak, which is the behaviour you would
  otherwise have to hand-tune.

* **Titles are repeated three times.** Term frequency is what it is; the title is
  the most reliable description of a product and the CKEditor description fields
  are frequently boilerplate ("I sell good products only"), so the title needs
  the weight.
"""

from __future__ import annotations

import logging
import re

import numpy as np
from django.utils.html import strip_tags
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r'\s+')
_NOISE_RE = re.compile(r'&nbsp;|&amp;|&quot;|&#39;', re.IGNORECASE)

#: Boilerplate defaults from the Product model — present on most rows and
#: therefore pure noise. Left in, TF-IDF would treat "I sell good products only"
#: as a meaningful shared feature.
_BOILERPLATE = {
    'i sell good products only',
    'we offer free standard shipping on all orders',
    'black',
}


def _clean(value) -> str:
    if not value:
        return ''
    text = strip_tags(str(value))
    text = _NOISE_RE.sub(' ', text)
    text = _WS_RE.sub(' ', text).strip()
    return '' if text.lower() in _BOILERPLATE else text


def _price_band(price, bands: list[float]) -> int:
    """Which price decile a product falls in — a coarse but useful similarity facet."""
    if price is None:
        return 0
    return int(np.searchsorted(bands, float(price)))


def build_documents(products) -> tuple[list[int], list[str]]:
    """
    Turn products into TF-IDF documents.

    `products` must be an iterable of Product rows with vendor / sub_category /
    brand selected. Returns (product_ids, documents) in matching order.
    """
    rows = list(products)
    if not rows:
        return [], []

    prices = np.array([float(p.price or 0) for p in rows], dtype=np.float64)
    positive = prices[prices > 0]
    bands = (
        list(np.quantile(positive, np.linspace(0.1, 0.9, 9)))
        if positive.size >= 10 else []
    )

    product_ids: list[int] = []
    documents: list[str] = []

    for product in rows:
        title = _clean(product.title)
        parts = [title, title, title]                     # title carries the most signal

        sub_category = getattr(product, 'sub_category', None)
        if sub_category:
            parts.append(_clean(sub_category.title))
            parts.append(f"__subcat_{sub_category.id}")
            category = getattr(sub_category, 'category', None)
            if category:
                parts.append(_clean(category.title))
                parts.append(f"__cat_{category.id}")
                main = getattr(category, 'main_category', None)
                if main:
                    parts.append(_clean(main.title))
                    parts.append(f"__maincat_{main.id}")

        brand = getattr(product, 'brand', None)
        if brand:
            parts.append(_clean(brand.title))
            parts.append(f"__brand_{brand.id}")

        vendor = getattr(product, 'vendor', None)
        if vendor:
            parts.append(f"__vendor_{vendor.id}")

        if product.product_type:
            parts.append(f"__ptype_{product.product_type}")

        parts.append(f"__price_p{_price_band(product.price, bands)}")

        # Descriptive fields, truncated: past a few hundred characters these are
        # shipping policy and returns boilerplate, which dilutes the signal.
        parts.append(_clean(product.features)[:400])
        parts.append(_clean(product.description)[:600])
        parts.append(_clean(product.specifications)[:400])

        document = ' '.join(part for part in parts if part)
        product_ids.append(product.id)
        documents.append(document.lower())

    return product_ids, documents


def fit_content_embeddings(
    products,
    dim: int = 48,
    max_features: int = 40_000,
    seed: int = 42,
) -> tuple[list[int], np.ndarray]:
    """
    Fit TF-IDF → SVD and return (product_ids, embeddings).

    Embeddings are L2-normalised, so cosine similarity between two products is a
    plain dot product. SVD rather than raw TF-IDF because the sparse vectors are
    both enormous and too literal: it puts "sneaker" and "trainers" near each
    other by virtue of the company they keep, which exact term matching never will.
    """
    product_ids, documents = build_documents(products)
    if len(product_ids) < 2:
        logger.warning("content: only %d product(s) — skipping content model", len(product_ids))
        return product_ids, np.zeros((len(product_ids), dim), dtype=np.float32)

    vectorizer = TfidfVectorizer(
        max_features=max_features,
        ngram_range=(1, 2),
        min_df=1,
        sublinear_tf=True,          # dampens repeated terms in long descriptions
        strip_accents='unicode',
        token_pattern=r'(?u)\b\w[\w_]+\b',   # keeps the __facet_ tokens intact
    )

    try:
        tfidf = vectorizer.fit_transform(documents)
    except ValueError as exc:
        logger.error("content: TF-IDF failed (%s) — returning zero vectors", exc)
        return product_ids, np.zeros((len(product_ids), dim), dtype=np.float32)

    # SVD cannot produce more components than the matrix has rank.
    components = int(min(dim, tfidf.shape[1] - 1, tfidf.shape[0] - 1))
    if components < 2:
        logger.warning("content: vocabulary too small for SVD — returning zero vectors")
        return product_ids, np.zeros((len(product_ids), dim), dtype=np.float32)

    svd = TruncatedSVD(n_components=components, random_state=seed, algorithm='randomized')
    reduced = svd.fit_transform(tfidf)

    # Pad back to the requested width so content and CF vectors stay interchangeable.
    if components < dim:
        reduced = np.hstack([reduced, np.zeros((reduced.shape[0], dim - components))])

    norms = np.linalg.norm(reduced, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    embeddings = (reduced / norms).astype(np.float32)

    logger.info(
        "content: embedded %d products · vocabulary %d · %d components · %.1f%% variance retained",
        len(product_ids), tfidf.shape[1], components,
        100 * float(svd.explained_variance_ratio_.sum()),
    )
    return product_ids, embeddings

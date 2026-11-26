"""
Product information sections: the seller names each one ("Processor",
"Ingredients", "Size & fit", ...) and writes it with the same rich editor as
the description. Shown on the product page as the "Product information" list.

Stored on Product.info_sections as [{"title": str, "body": html}]. The joined
HTML is also kept in Product.specifications so search, recommendations and
older readers of that field keep seeing the text (set_sections()).
"""

import html
import json
import re

from core.html_sanitize import clean_html

MAX_SECTIONS = 12
MAX_TITLE = 60
MAX_BODY = 20000          # characters of HTML per section
LEGACY_TITLE = 'Additional details'


class SectionError(ValueError):
    pass


def _plain(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', value or ''))).strip()


def clean_sections(raw):
    """
    The seller's sections, cleaned: titles trimmed, bodies sanitised, empty
    sections dropped. Raises SectionError with a message for the seller.
    """
    if raw in (None, ''):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise SectionError('Product information could not be read.')
    if not isinstance(raw, list):
        raise SectionError('Product information could not be read.')

    sections, seen = [], set()
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SectionError('Product information could not be read.')
        title = re.sub(r'\s+', ' ', str(item.get('title') or '')).strip()
        body = str(item.get('body') or '')
        if len(body) > MAX_BODY:
            raise SectionError(f'Section {i} is too long. Keep each section under {MAX_BODY:,} characters.')
        body = clean_html(body) or ''
        if not title and not _plain(body) and '<img' not in body:
            continue                      # an empty card the seller added and left
        if not title:
            raise SectionError(f'Section {i} needs a title (for example "Ingredients" or "Display").')
        if len(title) > MAX_TITLE:
            raise SectionError(f'Section title "{title[:20]}…" is too long. Keep titles under {MAX_TITLE} characters.')
        if not _plain(body) and '<img' not in body:
            raise SectionError(f'"{title}" is empty. Write something in it or remove it.')
        if title.lower() in seen:
            raise SectionError(f'There are two sections called "{title}". Merge them or rename one.')
        seen.add(title.lower())
        sections.append({'title': title, 'body': body})

    if len(sections) > MAX_SECTIONS:
        raise SectionError(f'Use at most {MAX_SECTIONS} sections.')
    return sections


def render_html(sections):
    """All sections as one HTML document (for search and older readers)."""
    return ''.join(f'<h3>{html.escape(s["title"])}</h3>{s["body"]}' for s in sections)


def set_sections(product, sections):
    """Store cleaned sections on the product (not saved)."""
    product.info_sections = sections
    product.specifications = render_html(sections)


def sections_for(product):
    """
    What the product page shows. Products saved before sections existed only
    have the old free-text field: show it as one section.
    """
    if product.info_sections:
        return product.info_sections
    legacy = product.specifications or ''
    if _plain(legacy) and _plain(legacy).lower() != 'black':     # 'Black' was the model's old default
        return [{'title': LEGACY_TITLE, 'body': legacy}]
    return []

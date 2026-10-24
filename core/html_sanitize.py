"""
core/html_sanitize.py
Clean rich text that sellers write (product description, features, ...) before
we store it. The storefront renders these fields as HTML, so anything a
browser would run — <script>, onerror=, javascript: links, <iframe>, inline
styles — is removed here, once, on the way in.

Uses nh3 (Rust "ammonia"). The allow-list matches what the CKEditor toolbar
can produce: text formatting, headings, lists, links, images, tables.
"""

import nh3

ALLOWED_TAGS = {
    'p', 'br', 'hr', 'span', 'div', 'strong', 'b', 'em', 'i', 'u', 's', 'sub', 'sup', 'mark', 'small',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'code',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'a', 'img', 'figure', 'figcaption',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
}

ALLOWED_ATTRIBUTES = {
    'a': {'href', 'title'},
    'img': {'src', 'alt', 'title', 'width', 'height'},
    'th': {'colspan', 'rowspan'},
    'td': {'colspan', 'rowspan'},
    'col': {'span'},
    'ol': {'start'},
}

ALLOWED_URL_SCHEMES = {'http', 'https', 'mailto'}


def clean_html(value):
    """Sanitised copy of `value`; None and '' pass through unchanged."""
    if not value:
        return value
    return nh3.clean(
        str(value),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel='noopener noreferrer nofollow',
        strip_comments=True,
    )

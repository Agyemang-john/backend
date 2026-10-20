"""
Page size for the storefront product listings (category, brand, search,
collection, deals, seller storefront).

Shoppers pick how many products a page shows with `?page_size=`. Only the
listed sizes are accepted, so a hand-edited URL can't ask for 10,000 rows and
cached pages stay few per listing; anything else falls back to the default.
"""

PAGE_SIZES = (30, 60, 90)
DEFAULT_PAGE_SIZE = PAGE_SIZES[0]


def listing_page_size(params):
    """`params` is request.GET / request.query_params."""
    try:
        size = int(params.get('page_size', DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return size if size in PAGE_SIZES else DEFAULT_PAGE_SIZE

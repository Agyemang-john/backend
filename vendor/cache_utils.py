from django.core.cache import cache

VENDOR_CACHE_KEY = "vendor_metadata:{slug}"

def invalidate_vendor_cache(slug: str):
    cache.delete(VENDOR_CACHE_KEY.format(slug=slug))

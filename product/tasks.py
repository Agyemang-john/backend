# tasks.py
import redis
from celery import shared_task
from .models import Product, Brand, Category, Sub_Category, FlashSale, ProductViewLog, ProductDailyStats, RecentlyViewedProduct
from .trending import compute_all_trending_scores
from celery import shared_task
from django.db.models import Sum
from order.models import CartItem
from django.conf import settings
from django.utils import timezone
from django.db.models import F  

import logging

logger = logging.getLogger(__name__)

@shared_task(ignore_result=True)
def flush_view_counts():
    """
    Drains Redis view-count buffers (views:buf:{id}) into the DB in one bulk pass.

    Flow:
      - buffer_view_count() does INCR views:buf:{id} on every new unique view (no DB hit).
      - This task runs every 3 minutes via Celery Beat, SCANs for those keys,
        reads + deletes each with GETDEL (atomic), then does a single bulk UPDATE
        per product.  One DB round-trip per batch instead of one per view.
    """
    try:
        from django_redis import get_redis_connection
        conn = get_redis_connection("default")
    except Exception:
        logger.warning("flush_view_counts: Redis unavailable, skipping")
        return

    updates: dict[int, int] = {}
    cursor = 0

    # SCAN is non-blocking and safe on large keyspaces
    while True:
        try:
            cursor, keys = conn.scan(cursor, match="views:buf:*", count=500)
        except Exception as e:
            logger.error("flush_view_counts: SCAN error: %s", e)
            break

        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            try:
                product_id = int(key.split(":")[-1])
            except ValueError:
                continue

            try:
                # GETDEL: atomic get-and-delete (Redis ≥ 6.2)
                # Falls back to GET+DEL pipeline for older Redis
                try:
                    raw = conn.getdel(key)
                except AttributeError:
                    pipe = conn.pipeline()
                    pipe.get(key)
                    pipe.delete(key)
                    raw, _ = pipe.execute()

                if raw:
                    delta = int(raw)
                    updates[product_id] = updates.get(product_id, 0) + delta
            except Exception as e:
                logger.error("flush_view_counts: error on key %s: %s", key, e)

        if cursor == 0:
            break

    if not updates:
        return

    # One UPDATE per product — could be further batched with a CASE WHEN if needed
    from django.db import transaction
    with transaction.atomic():
        for product_id, delta in updates.items():
            Product.objects.filter(id=product_id).update(views=F('views') + delta)

    logger.info("flush_view_counts: flushed %d product(s), total delta %d",
                len(updates), sum(updates.values()))

@shared_task(ignore_result=True)
def expire_flash_sales():
    """
    Deactivates flash sales whose end_time has passed.
    Runs every 60 seconds via Celery Beat.
    Also invalidates the live flash-sales cache so the next request
    returns fresh data immediately.
    """
    from django.utils import timezone as tz
    from django.core.cache import cache
    expired = FlashSale.objects.filter(is_active=True, end_time__lt=tz.now())
    count = expired.update(is_active=False)
    if count:
        cache.delete("flash_sales_live")
        logger.info(f"Expired {count} flash sale(s).")


@shared_task(bind=True, max_retries=3, default_retry_delay=120, ignore_result=True)
def update_trending_scores(self):
    """
    Recalculate trending_score for all published products in bulk.
    Runs every 4 hours via Celery Beat.

    Uses multi-signal scoring:
      - Unique non-bot views: last 24 h (live), last 7 d, last 30 d
      - Cart adds: last 7 days
      - Confirmed purchases: last 7 days
      - Quality baseline: avg_rating × log1p(review_count)
    """
    try:
        count = compute_all_trending_scores()
        logger.info("update_trending_scores: scored %d products", count)
        # Bust the trending endpoint cache so the next request gets fresh data
        from django.core.cache import cache
        cache.delete("top_trending_product")
        cache.delete("top_trending_products")
    except Exception as exc:
        logger.error("update_trending_scores failed: %s", exc)
        raise self.retry(exc=exc)



# generate_fbt() lived here: an mlxtend/apriori job that rebuilt
# FrequentlyBoughtTogether every six hours. Superseded by
# recommendation.similarity.build_co_purchase(), which computes the same
# pairings with cosine normalisation — so a best-seller stops appearing as a
# "complement" to everything — and without loading pandas into the worker.


@shared_task(ignore_result=True)
def flush_brand_view_counts():
    """
    Drains Redis brand view-count buffers (brand:views:buf:{id}) into Brand.views.
    Runs every 3 minutes via Celery Beat — same pattern as flush_view_counts().
    """
    try:
        from django_redis import get_redis_connection
        conn = get_redis_connection("default")
    except Exception:
        logger.warning("flush_brand_view_counts: Redis unavailable, skipping")
        return

    updates: dict[int, int] = {}
    cursor = 0

    while True:
        try:
            cursor, keys = conn.scan(cursor, match="brand:views:buf:*", count=500)
        except Exception as e:
            logger.error("flush_brand_view_counts: SCAN error: %s", e)
            break

        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            try:
                brand_id = int(key.split(":")[-1])
            except ValueError:
                continue
            try:
                try:
                    raw = conn.getdel(key)
                except AttributeError:
                    pipe = conn.pipeline()
                    pipe.get(key)
                    pipe.delete(key)
                    raw, _ = pipe.execute()
                if raw:
                    updates[brand_id] = updates.get(brand_id, 0) + int(raw)
            except Exception as e:
                logger.error("flush_brand_view_counts: error on key %s: %s", key, e)

        if cursor == 0:
            break

    if not updates:
        return

    from django.db import transaction
    with transaction.atomic():
        for brand_id, delta in updates.items():
            Brand.objects.filter(id=brand_id).update(views=F('views') + delta)

    logger.info("flush_brand_view_counts: flushed %d brand(s), total delta %d",
                len(updates), sum(updates.values()))


@shared_task(ignore_result=True)
def flush_subcategory_view_counts():
    """
    Drains Redis subcategory view-count buffers (cat:views:buf:{id}) into Sub_Category.views.
    Runs every 3 minutes via Celery Beat — same pattern as flush_view_counts().
    """
    try:
        from django_redis import get_redis_connection
        conn = get_redis_connection("default")
    except Exception:
        logger.warning("flush_subcategory_view_counts: Redis unavailable, skipping")
        return

    updates: dict[int, int] = {}
    cursor = 0

    while True:
        try:
            cursor, keys = conn.scan(cursor, match="cat:views:buf:*", count=500)
        except Exception as e:
            logger.error("flush_subcategory_view_counts: SCAN error: %s", e)
            break

        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            try:
                subcategory_id = int(key.split(":")[-1])
            except ValueError:
                continue
            try:
                try:
                    raw = conn.getdel(key)
                except AttributeError:
                    pipe = conn.pipeline()
                    pipe.get(key)
                    pipe.delete(key)
                    raw, _ = pipe.execute()
                if raw:
                    updates[subcategory_id] = updates.get(subcategory_id, 0) + int(raw)
            except Exception as e:
                logger.error("flush_subcategory_view_counts: error on key %s: %s", key, e)

        if cursor == 0:
            break

    if not updates:
        return

    from django.db import transaction
    with transaction.atomic():
        for subcategory_id, delta in updates.items():
            Sub_Category.objects.filter(id=subcategory_id).update(views=F('views') + delta)

    logger.info("flush_subcategory_view_counts: flushed %d subcategory(ies), total delta %d",
                len(updates), sum(updates.values()))


@shared_task(ignore_result=True)
def flush_category_view_counts():
    """
    Drains Redis category view-count buffers (maincat:views:buf:{id}) into Category.views.
    Runs every 3 minutes via Celery Beat — same pattern as flush_subcategory_view_counts().
    """
    try:
        from django_redis import get_redis_connection
        conn = get_redis_connection("default")
    except Exception:
        logger.warning("flush_category_view_counts: Redis unavailable, skipping")
        return

    updates: dict[int, int] = {}
    cursor = 0

    while True:
        try:
            cursor, keys = conn.scan(cursor, match="maincat:views:buf:*", count=500)
        except Exception as e:
            logger.error("flush_category_view_counts: SCAN error: %s", e)
            break

        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            try:
                category_id = int(key.split(":")[-1])
            except ValueError:
                continue
            try:
                try:
                    raw = conn.getdel(key)
                except AttributeError:
                    pipe = conn.pipeline()
                    pipe.get(key)
                    pipe.delete(key)
                    raw, _ = pipe.execute()
                if raw:
                    updates[category_id] = updates.get(category_id, 0) + int(raw)
            except Exception as e:
                logger.error("flush_category_view_counts: error on key %s: %s", key, e)

        if cursor == 0:
            break

    if not updates:
        return

    from django.db import transaction
    with transaction.atomic():
        for category_id, delta in updates.items():
            Category.objects.filter(id=category_id).update(views=F('views') + delta)

    logger.info("flush_category_view_counts: flushed %d category(ies), total delta %d",
                len(updates), sum(updates.values()))


@shared_task
def update_category_engagement_scores():
    """
    Category engagement score — five signals, ordered by buyer intent:

      3.0 × direct category page visits  (user explicitly browsed this category)
      1.5 × subcategory page visits       (browsed a sub-section of this category)
      0.1 × product views                 (breadth / awareness signal)
      5.0 × cart adds                     (strong purchase intent)
     10.0 × purchases                     (confirmed conversion — highest weight)

    Busts the TopEngagedCategoryView cache so the homepage reflects the fresh score.
    """
    from order.models import CartItem, OrderProduct
    from django.core.cache import cache as _cache

    for category in Category.objects.all():
        category_views = category.views

        sub_views = Sub_Category.objects.filter(
            category=category
        ).aggregate(total=Sum('views'))['total'] or 0

        product_views = Product.published.filter(
            sub_category__category=category
        ).aggregate(total=Sum('views'))['total'] or 0

        cart_adds = CartItem.objects.filter(
            product__sub_category__category=category
        ).count()

        purchases = OrderProduct.objects.filter(
            product__sub_category__category=category
        ).count()

        score = round(
            (category_views * 3.0) +
            (sub_views      * 1.5) +
            (product_views  * 0.1) +
            (cart_adds      * 5.0) +
            (purchases      * 10.0),
            2,
        )
        category.engagement_score = score
        category.save(update_fields=['engagement_score'])

    _cache.delete("top_engaged_category")
    _cache.delete("homepage_main_v1")
    return "Category engagement scores updated."


@shared_task
def update_brand_engagement_scores():
    """
    Brand score = brand page views + cart adds + actual purchases.

    Three signals, three different levels of buyer intent:
      - brand page view  → awareness / interest          (weight 1×)
      - cart add         → strong purchase intent        (weight 5×)
      - confirmed order  → highest intent / conversion   (weight 10×)

    All three now use the properly-tracked Brand.views field (fed by
    flush_brand_view_counts) rather than a stale integer.
    """
    from order.models import CartItem, OrderProduct

    for brand in Brand.objects.all():
        brand_views   = brand.views
        cart_mentions = CartItem.objects.filter(product__brand=brand).count()
        order_count   = OrderProduct.objects.filter(product__brand=brand).count()

        score = round(
            (brand_views   * 1.0) +
            (cart_mentions * 5.0) +
            (order_count   * 10.0),
            2,
        )
        brand.engagement_score = score
        brand.save(update_fields=['engagement_score'])

    return "Brand engagement scores updated."


@shared_task
def update_subcategory_engagement_scores():
    """
    Subcategory engagement score — four signals, ordered by buyer intent:

      3.0 × direct subcategory page visits  (user explicitly browsed this subcategory)
      0.1 × product views                   (breadth / awareness signal)
      5.0 × cart adds                       (strong purchase intent)
     10.0 × purchases                       (confirmed conversion — highest weight)
    """
    from order.models import CartItem, OrderProduct

    for subcategory in Sub_Category.objects.all():
        sub_views = subcategory.views

        product_views = Product.published.filter(
            sub_category=subcategory
        ).aggregate(total=Sum('views'))['total'] or 0

        cart_adds = CartItem.objects.filter(product__sub_category=subcategory).count()
        purchases = OrderProduct.objects.filter(product__sub_category=subcategory).count()

        score = round(
            (sub_views    * 3.0) +
            (product_views * 0.1) +
            (cart_adds     * 5.0) +
            (purchases     * 10.0),
            2,
        )
        subcategory.engagement_score = score
        subcategory.save(update_fields=['engagement_score'])

    return "Subcategory engagement scores updated."

    return "Subcategory engagement scores updated."



from celery import shared_task
from product.models import Product
from elasticsearch8 import Elasticsearch
from elasticsearch8.helpers import bulk
import time
from django.utils import timezone
from django.conf import settings

@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def index_products_task(self, last_run=None):
    def connect_to_elasticsearch():
        max_retries = 5
        retry_delay = 5
        for attempt in range(max_retries):
            try:
                es = Elasticsearch(
                    hosts=[settings.ELASTICSEARCH_URL],
                    verify_certs=True,
                    request_timeout=30
                )
                if not es.ping():
                    raise ConnectionError("Elasticsearch is not running or unreachable.")
                return es
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"Attempt {attempt + 1} failed: {str(e)}. Retrying...")
                    time.sleep(retry_delay)
                else:
                    print(f"Failed to connect after {max_retries} attempts")
                    return None

    def setup_index(es):
        try:
            if not es.indices.exists(index="products"):
                es.indices.create(
                    index="products",
                    body={
                        "settings": {
                            "number_of_shards": 1,  # Single shard
                            "number_of_replicas": 1,  # Single replica
                            "analysis": {
                                "analyzer": {
                                    "custom_analyzer": {
                                        "type": "custom",
                                        "tokenizer": "standard",
                                        "filter": ["lowercase", "asciifolding"]
                                    }
                                }
                            }
                        },
                        "mappings": {
                            "properties": {
                                "title": {
                                    "type": "text",
                                    "analyzer": "custom_analyzer",
                                    "fields": {"keyword": {"type": "keyword"}}
                                },
                                "description": {"type": "text", "analyzer": "custom_analyzer"},
                                "price": {"type": "float"},
                                "status": {"type": "keyword"},
                                "vendor": {
                                    "type": "text",
                                    "fields": {"keyword": {"type": "keyword"}}
                                },
                                "brand": {
                                    "type": "text",
                                    "fields": {"keyword": {"type": "keyword"}}
                                },
                                "sub_category": {"type": "keyword"},
                                "variants": {
                                    "type": "nested",
                                    "properties": {
                                        "color": {"type": "keyword"},
                                        "size": {"type": "keyword"}
                                    }
                                }
                            }
                        }
                    }
                )
        except Exception as e:
            print(f'Failed to setup index: {str(e)}')
            raise

    def index_products(es):
        try:
            query = Product.published.all()
            if last_run:
                try:
                    last_run_dt = timezone.datetime.fromisoformat(last_run)
                    query = query.filter(updated__gte=last_run_dt)
                except ValueError as e:
                    print(f"Invalid last_run format: {str(e)}")

            products = query
            total = products.count()
            if total == 0:
                print("No products to index")
                return

            actions = []
            for product in products:
                try:
                    doc = {
                        "id": product.id,
                        "title": product.title,
                        "description": str(product.description),
                        "price": float(product.price),
                        "status": product.status,
                        "vendor": getattr(product.vendor, 'name', ''),
                        "brand": getattr(getattr(product, 'brand', None), 'title', ''),
                        "sub_category": getattr(getattr(product, 'sub_category', None), 'title', ''),
                        "variants": [
                            {
                                "color": getattr(v.color, 'name', 'Unknown'),
                                "size": getattr(v.size, 'name', 'Unknown')
                            }
                            for v in product.variants.all()
                        ]
                    }
                    actions.append({
                        "_index": "products",
                        "_id": product.id,
                        "_source": doc
                    })
                except Exception as e:
                    print(f"Failed to prepare product {product.id}: {str(e)}")

            if actions:
                success, errors = bulk(es, actions, chunk_size=50, raise_on_error=False)
                print(f"Indexed {success}/{total} products successfully")
                if errors:
                    print(f"Errors occurred: {errors}")
            else:
                print("No products to index")
        except Exception as e:
            print(f'Failed to index products: {str(e)}')
            raise

    try:
        es = connect_to_elasticsearch()
        if not es:
            raise Exception("Elasticsearch connection failed")
        setup_index(es)
        index_products(es)
    except Exception as e:
        print(f"Task failed: {str(e)}")
        self.retry(exc=e)


# ── View analytics tasks ──────────────────────────────────────────────────────

@shared_task(ignore_result=True)
def log_view_event(product_id, visitor_key, user_id, is_bot, is_returning, device_type, date_str):
    """
    Write one ProductViewLog row. Called async from track_view() so the
    product detail request is never blocked by a DB insert.
    """
    from datetime import date as _date
    try:
        parsed_date = _date.fromisoformat(date_str)
        ProductViewLog.objects.create(
            product_id=product_id,
            visitor_key=visitor_key,
            user_id=user_id,
            is_bot=is_bot,
            is_returning=is_returning,
            device_type=device_type,
            date=parsed_date,
        )
    except Exception as e:
        logger.error("log_view_event: failed product=%s err=%s", product_id, e)


@shared_task(ignore_result=True)
def aggregate_daily_stats():
    """
    Materialise yesterday's ProductViewLog rows into ProductDailyStats.
    Runs at midnight UTC via Celery Beat. Skips today so in-flight rows
    are not double-counted.
    """
    from django.utils import timezone as tz
    from django.db.models import Count, Q
    from datetime import timedelta as _td
    yesterday = (tz.now() - _td(days=1)).date()

    rows = list(
        ProductViewLog.objects
        .filter(date=yesterday)
        .values('product_id')
        .annotate(
            total=Count('id'),
            unique=Count('id', filter=Q(is_returning=False, is_bot=False)),
            returning=Count('id', filter=Q(is_returning=True, is_bot=False)),
            bots=Count('id', filter=Q(is_bot=True)),
        )
    )
    for row in rows:
        ProductDailyStats.objects.update_or_create(
            product_id=row['product_id'],
            date=yesterday,
            defaults={
                'total_views':     row['total'],           # all views including bots
                'unique_views':    row['unique'],          # non-bot new visitors
                'returning_views': row['returning'],       # non-bot returning visitors
                'bot_views':       row['bots'],
            },
        )
    logger.info("aggregate_daily_stats: aggregated %d product(s) for %s", len(rows), yesterday)


@shared_task(ignore_result=True)
def sync_recently_viewed_db(user_id, product_id):
    """
    Upsert a RecentlyViewedProduct row for authenticated users.
    Called from ProductDetailAPIView on EVERY page load (not gated by analytics
    dedup) so viewed_at is always bumped and the most-recent product stays first.
    """
    try:
        obj, created = RecentlyViewedProduct.objects.get_or_create(
            user_id=user_id,
            product_id=product_id,
        )
        if not created:
            # Touch viewed_at (auto_now field) by calling save()
            RecentlyViewedProduct.objects.filter(
                user_id=user_id, product_id=product_id
            ).update(viewed_at=timezone.now())
    except Exception as e:
        logger.error("sync_recently_viewed_db: user=%s product=%s err=%s", user_id, product_id, e)


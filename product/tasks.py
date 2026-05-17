# tasks.py
import redis
from celery import shared_task
from .models import Product, FrequentlyBoughtTogether, Brand, Category, Sub_Category, FlashSale, ProductViewLog, ProductDailyStats, RecentlyViewedProduct
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



import pandas as pd
from mlxtend.frequent_patterns import apriori, association_rules
from celery import shared_task
from order.models import Order

@shared_task
def generate_fbt():
    # Step 1: Gather product transactions
    orders = Order.objects.prefetch_related('order_products__product')
    transactions = [
        list({item.product.id for item in order.order_products.all()})
        for order in orders
        if order.order_products.exists()
    ]

    if not transactions:
        return "No transactions to process"

    # Step 2: One-hot encoding (ensuring no duplicates)
    df = pd.DataFrame(transactions)
    df = df.apply(lambda x: pd.Series(1, index=pd.unique(x.dropna())), axis=1).fillna(0)

    # Step 3: Run Apriori
    frequent_itemsets = apriori(df, min_support=0.01, use_colnames=True)
    if frequent_itemsets.empty:
        return "No frequent itemsets found"

    rules = association_rules(frequent_itemsets, metric="lift", min_threshold=1.0)
    if rules.empty:
        return "No association rules generated"

    # Step 4: Save rules
    FrequentlyBoughtTogether.objects.all().delete()
    for _, row in rules.iterrows():
        for a in row["antecedents"]:
            for c in row["consequents"]:
                if a != c:
                    FrequentlyBoughtTogether.objects.get_or_create(
                        product_id=a, recommended_id=c
                    )

    return f"Generated {rules.shape[0]} association rules"


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


@shared_task
def update_category_engagement_scores():
    """
    Category score = direct subcategory page views + product views inside the category.

    Direct subcategory page visits (tracked since the user explicitly browsed here)
    are weighted more heavily than cumulative product views, which are a secondary
    breadth signal. The parent Category row gets the aggregate of all its sub-categories.
    """
    for category in Category.objects.all():
        # Sum of direct page visits across every sub-category of this category
        sub_views = Sub_Category.objects.filter(
            category=category
        ).aggregate(total=Sum('views'))['total'] or 0

        # Sum of all product views for products listed in this category
        product_views = Product.published.filter(
            sub_category__category=category
        ).aggregate(total=Sum('views'))['total'] or 0

        # Subcategory page visits are a stronger intent signal than product view counts
        score = round((sub_views * 1.5) + (product_views * 0.1), 2)
        category.engagement_score = score
        category.save(update_fields=['engagement_score'])

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
    Subcategory score = direct page views + cart adds + actual purchases.

    Direct page visits now come from the Redis-buffered tracker wired into
    CategoryProductListView, so Sub_Category.views reflects real human traffic.
    Cart adds and purchases are stronger conversion signals layered on top.
    """
    from order.models import CartItem, OrderProduct

    for subcategory in Sub_Category.objects.all():
        views         = subcategory.views
        cart_mentions = CartItem.objects.filter(product__sub_category=subcategory).count()
        order_count   = OrderProduct.objects.filter(product__sub_category=subcategory).count()

        score = round(
            (views         * 1.0) +
            (cart_mentions * 5.0) +
            (order_count   * 10.0),
            2,
        )
        subcategory.engagement_score = score
        subcategory.save(update_fields=['engagement_score'])

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
            query = Product.objects.filter(status="published")
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


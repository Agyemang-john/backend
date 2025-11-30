# tasks.py
import redis
from celery import shared_task
from .models import Product, FrequentlyBoughtTogether, Brand, Category, Sub_Category, ProductView
from .trending import calculate_trending_score
from celery import shared_task
from django.db.models import Sum
from order.models import CartItem
from django.conf import settings
from django.utils import timezone
from django.db.models import F  

import logging

logger = logging.getLogger(__name__)

@shared_task(ignore_result=True)
def increment_product_view_count(product_id: int):
    from .models import Product
    Product.objects.filter(id=product_id).update(views=F('views') + 1)

@shared_task
def clear_product_views():
    try:
        twenty_four_hours_ago = timezone.now() - timezone.timedelta(hours=24)
        batch_size = 200
        while True:
            batch = ProductView.objects.filter(
                created_at__lte=twenty_four_hours_ago
            )[:batch_size]
            if not batch.exists():
                break
            deleted_count, _ = batch.delete()
            logger.info(f"Deleted {deleted_count} ProductView records in batch")
    except Exception as e:
        logger.error(f"Error deleting ProductView records: {str(e)}")

@shared_task
def update_trending_scores():
    for product in Product.objects.filter(status="published"):
        score = calculate_trending_score(product)
        product.trending_score = score
        product.save()



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


@shared_task
def update_category_engagement_scores():
    for category in Category.objects.all():
        total_views = Product.published.filter(
            sub_category__category=category
        ).aggregate(score=Sum('views'))['score'] or 0

        category.engagement_score = total_views
        category.save()
    return "Category engagement scores updated."


@shared_task
def update_brand_engagement_scores():
    brands = Brand.objects.all()

    for brand in brands:
        views = brand.views

        # Count how many times this brand’s products appear in cart items
        cart_mentions = CartItem.objects.filter(
            product__brand=brand
        ).count()

        # Example: weighted formula
        score = (0.6 * views) + (0.4 * cart_mentions)

        brand.engagement_score = round(score, 2)
        brand.save()

    return "Engagement scores updated."


@shared_task
def update_subcategory_engagement_scores():
    subcategories = Sub_Category.objects.all()

    for subcategory in subcategories:
        # Subcategory's direct view count
        views = subcategory.views

        # Count how many times products in this subcategory appear in cart items
        cart_mentions = CartItem.objects.filter(
            product__sub_category=subcategory
        ).count()

        # Weighted engagement score
        score = (0.6 * views) + (0.4 * cart_mentions)

        subcategory.engagement_score = round(score, 2)
        subcategory.save()

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


# product/management/commands/index_products.py
from django.core.management.base import BaseCommand
from product.models import Product
from elasticsearch8 import Elasticsearch
import time
from django.db.models import Q

class Command(BaseCommand):
    help = 'Index products into Elasticsearch with improved search capabilities'

    def handle(self, *args, **options):
        # Connection setup with retries
        es = self.connect_to_elasticsearch()
        if not es:
            return

        # Create or update index with better search configuration
        self.setup_index(es)

        # Index products with improved data handling
        self.index_products(es)

    def connect_to_elasticsearch(self):
        max_retries = 5
        retry_delay = 5
        for attempt in range(max_retries):
            try:
                es = Elasticsearch(hosts=["http://localhost:9200"], request_timeout=30)
                if not es.ping():
                    raise ConnectionError("Elasticsearch is not running or unreachable.")
                return es
            except Exception as e:
                if attempt < max_retries - 1:
                    self.stdout.write(f"Attempt {attempt + 1} failed: {str(e)}. Retrying...")
                    time.sleep(retry_delay)
                else:
                    self.stdout.write(self.style.ERROR(f"Failed to connect after {max_retries} attempts"))
                    return None

    def setup_index(self, es):
        try:
            # Delete existing index if it exists
            if es.indices.exists(index="products"):
                es.indices.delete(index="products")

            # Create new index with better search configuration
            es.indices.create(
                index="products",
                body={
                    "settings": {
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
                                "fields": {
                                    "keyword": {"type": "keyword"}
                                }
                            },
                            "description": {
                                "type": "text",
                                "analyzer": "custom_analyzer"
                            },
                            "price": {"type": "float"},
                            "status": {"type": "keyword"},
                            "vendor": {
                                "type": "text",
                                "fields": {
                                    "keyword": {"type": "keyword"}
                                }
                            },
                            "brand": {
                                "type": "text",
                                "fields": {
                                    "keyword": {"type": "keyword"}
                                }
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
            self.stdout.write(self.style.ERROR(f'Failed to setup index: {str(e)}'))
            raise

    def index_products(self, es):
        try:
            # Only index published products
            products = Product.objects.filter(status="published")
            total = products.count()
            success = 0

            for i, product in enumerate(products, 1):
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
                    es.index(index="products", id=product.id, body=doc)
                    success += 1
                    
                    # Progress update
                    if i % 100 == 0 or i == total:
                        self.stdout.write(f"Indexed {i}/{total} products...")
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"Failed to index product {product.id}: {str(e)}"))

            self.stdout.write(self.style.SUCCESS(f'Successfully indexed {success}/{total} products'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Failed to index products: {str(e)}'))
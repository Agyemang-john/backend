# tasks.py
from celery import shared_task
from .models import Product, FrequentlyBoughtTogether, Brand, Category, Sub_Category
from .trending import calculate_trending_score
from celery import shared_task
from django.db.models import Sum
from order.models import CartItem

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

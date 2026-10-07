"""
product/models.py
Data models for the product catalog:
- Main_Category, Category, Sub_Category: three-level navigation hierarchy
- Brand, Type: product classification
- DeliveryOption, ProductDeliveryOption: shipping methods per product
- Product: core product model with full-text search, trending scores, variants
- Variants: size/color/price variants of a product
- ProductImages, VariantImage: product and variant image galleries
- ProductReview: customer reviews with ratings
- Wishlist: saved products per user
- Color, Size: attribute models for variants
"""

from django.db import models
from shortuuid.django_fields import ShortUUIDField
from django.utils.html import mark_safe
from django.utils.text import slugify
from vendor.models import *
from core.models import *
from .utils import *
from datetime import timedelta
from address.models import Country
from django_ckeditor_5.fields import CKEditor5Field
from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.search import SearchVector
from django.db.models import F, Sum
from django.utils import timezone
import os
import uuid
   
####################### CATEGORIES MODEL ##################

class Main_Category(models.Model):
    title = models.CharField(max_length=100, unique=True, default="Food")
    slug = models.SlugField(max_length=100, unique=True)
    date = models.DateTimeField(auto_now_add=True, null=True,blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "maincategory"
        verbose_name_plural = "maincategories"

    def __str__(self):
        return self.title

    
    def save(self, *args, **kwargs):
        self.slug = slugify(self.title, allow_unicode=True)
        super(Main_Category, self).save(*args, **kwargs)



class Category(models.Model):
    title = models.CharField(max_length=100, unique=True, default="Food")
    slug = models.SlugField(max_length=100, unique=True)
    main_category = models.ForeignKey(Main_Category, on_delete=models.CASCADE, null=True)
    main_image = models.ImageField(upload_to="category/", default="category.jpg")
    image = models.ImageField(upload_to="category/", default="category.jpg")
    date = models.DateTimeField(auto_now_add=True, null=True,blank=True)
    views = models.PositiveIntegerField(default=0)
    engagement_score = models.FloatField(default=0.0)

    class Meta:
        verbose_name = "category"
        verbose_name_plural = "categories"

    def category_image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.image.url))

    def __str__(self):
        return self.main_category.title + " -- " + self.title
    
    def save(self, *args, **kwargs):
        self.slug = slugify(self.title, allow_unicode=True)
        super(Category, self).save(*args, **kwargs)
    
class Sub_Category(models.Model):
    title = models.CharField(max_length=100, unique=True, default="Food")
    slug = models.SlugField(max_length=100, unique=True)
    category = models.ForeignKey(Category, related_name='category', on_delete=models.CASCADE, null=True)
    image = models.ImageField(upload_to="subcategory/", default="subcategory.jpg")
    views = models.PositiveIntegerField(default=0)
    engagement_score = models.FloatField(default=0.0)
    date = models.DateTimeField(auto_now_add=True, null=True,blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "subcategory"
        verbose_name_plural = "subcategories"
    
    def save(self, *args, **kwargs):
        self.slug = slugify(self.title, allow_unicode=True)
        super(Sub_Category, self).save(*args, **kwargs)

   
    def product_count(self):
        return Product.published.filter(sub_category=self.id).count()

    def subcategory_image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.image.url))

    def __str__(self):
        return self.category.main_category.title + " -- " + self.category.title + " -- " + self.title

class PublishedManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(
            status='published',
            vendor__shop_paused=False,
            vendor__is_suspended=False,
        )

def vendor_directory_path(instance, filename):
    return 'vendors/vendor_{0}/{1}'.format(instance.vendor.id, filename)

def user_directory_path(instance, filename):
    return 'users/user_{0}/{1}'.format(instance.user.id, filename)

class Brand(models.Model):
    title = models.CharField(max_length=20, unique=True, default="Adepa")
    slug = models.SlugField(max_length=100, null=True, unique=True)
    image = models.ImageField(upload_to="brands/", default="brand.jpg")
    views = models.PositiveIntegerField(default=0)
    engagement_score = models.FloatField(default=0.0)
    date = models.DateTimeField(auto_now_add=True, null=True,blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "brand"
        verbose_name_plural = "brands"

    def __str__(self):
        return self.title
    
    def brand_count(self):
        return Product.published.filter(brand=self.id).count()
    
    def save(self, *args, **kwargs):
        self.slug = slugify(self.title, allow_unicode=True)
        super(Brand, self).save(*args, **kwargs)


class Type(models.Model):
    name = models.CharField(max_length=20, unique=True, default="Adepa")

    def __str__(self):
        return self.name


class DeliveryOption(models.Model):
    LOCAL = 'local'
    INTERNATIONAL = 'international'
    TYPE_CHOICES = [
        (LOCAL, 'Local'),
        (INTERNATIONAL, 'International'),
    ]

    name = models.CharField(max_length=100)
    description = models.TextField()
    min_days = models.IntegerField(default=0)
    max_days = models.IntegerField(default=0)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=LOCAL)
    provider = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def get_delivery_date_range(self, reference_date=None, dynamic_min_days=None, dynamic_max_days=None):
        """
        Calculate the delivery date range based on the provided reference date or now.
        Supports overriding with dynamic values from third-party API for international.
        Returns a formatted string for user display (e.g., 'Today', 'Tomorrow', or 'Sep 25 to Sep 27, 2025').
        """
        now = reference_date or timezone.now()
        today = now.date()

        use_min_days = dynamic_min_days if dynamic_min_days is not None else self.min_days
        use_max_days = dynamic_max_days if dynamic_max_days is not None else self.max_days

        if self.name.lower() in ["same-day delivery", "same-day"] and self.type == self.LOCAL:
            cutoff_hour = 10
            if now.hour >= cutoff_hour:
                delivery_date = today + timedelta(days=1)
                return delivery_date.strftime("%b %d, %Y")
            return "Today"

        min_date = today + timedelta(days=use_min_days)
        max_date = today + timedelta(days=use_max_days)

        if max_date < today:
            logger.warning(f"Delivery option {self.name} is overdue (max_date: {max_date})")
            return f"Overdue (expected by {max_date.strftime('%b %d, %Y')})"

        from_date = "Today" if min_date == today else min_date.strftime("%b %d, %Y")
        to_date = "Today" if max_date == today else max_date.strftime("%b %d, %Y")

        if from_date == to_date:
            return f"{from_date}"
        return f"{from_date} to {to_date}"

    def get_delivery_status(self, reference_date=None, dynamic_min_days=None, dynamic_max_days=None):
        """
        Determine the delivery status based on the current date and delivery range.
        Supports dynamic overrides for international.
        Returns: 'TODAY', 'TOMORROW', 'IN X DAYS', 'ONGOING', 'OVERDUE', or 'UPCOMING'.
        """
        now = reference_date or timezone.now()
        today = now.date()

        use_min_days = dynamic_min_days if dynamic_min_days is not None else self.min_days
        use_max_days = dynamic_max_days if dynamic_max_days is not None else self.max_days

        delivery_range = self.get_delivery_date_range(
            reference_date, dynamic_min_days=use_min_days, dynamic_max_days=use_max_days
        )

        if isinstance(delivery_range, str):
            if "Overdue" in delivery_range:
                return "OVERDUE"
            return delivery_range.upper()

        min_date = today + timedelta(days=use_min_days)
        max_date = today + timedelta(days=use_max_days)

        if max_date < today:
            return "OVERDUE"
        elif min_date > today:
            days_until_start = (min_date - today).days
            return "TOMORROW" if days_until_start == 1 else f"IN {days_until_start} DAYS"
        elif min_date <= today <= max_date:
            return "TODAY" if min_date == max_date == today else "ONGOING"
        return "UPCOMING"

class Product(models.Model):
    STATUS = (
        ("draft", "Draft"),
        ("disabled", "Disabled"),
        ("rejected", "Rejected"),
        ("in_review", "In Review"),
        ("published", "Published"),
    )

    VARIANTS=(
        ('None','None'),
        ('Size','Size'),
        ('Color','Color'),
        ('Size-Color','Size-Color'),
    )
    
    OPTIONS=(
        ('book','Book'),
        ('grocery','Grocery'),
        ('refurbished','Refurbished'),
        ('new','New'),
        ('used','Used'),
    )
    slug = models.SlugField(max_length=150, unique=True)
    sub_category = models.ForeignKey('Sub_Category', on_delete=models.SET_NULL, null=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, related_name="product")
    variant = models.CharField(max_length=20, choices=VARIANTS, default='None')
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True)
    status = models.CharField(max_length=50, choices=STATUS, default='in_review')
    title = models.CharField(max_length=150, unique=True, help_text="Don't add color or size type, make sure each word starts with a capital letter ")
    image = models.ImageField(upload_to=vendor_directory_path, help_text="Main image of the product", null=True, blank=True)
    video = models.FileField(upload_to="video/%y", null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default="1.99", help_text="Base currency in GHS (e.g 70)")
    old_price = models.DecimalField(max_digits=10, decimal_places=2, default="2.99", help_text="Base currency in GHS (e.g 50)")
    features = CKEditor5Field(null=True, blank=True, default="Black")
    description = CKEditor5Field(null=True, blank=True, default="I sell good products only")
    specifications = CKEditor5Field(null=True, blank=True, default="Black")
    delivery_returns = CKEditor5Field(null=True, blank=True, default="We offer free standard shipping on all orders")
    available_in_regions = models.ManyToManyField(Country, blank=True, related_name='products')
    product_type = models.CharField(max_length=50, choices=OPTIONS, null=True, blank=True, default='new')
    total_quantity = models.PositiveIntegerField(default="100", null=True, blank=True)
    weight = models.FloatField(default=1.0)  # Weight in kg, or volume in liters
    volume = models.FloatField(default=1.0)  # Volume in cubic meters, if applicable
    life = models.CharField(max_length=100, default="100", null=True, blank=True )
    mfd = models.DateTimeField(auto_now_add=False, null=True, blank=True)
    return_period_days = models.PositiveIntegerField(default=0)
    warranty_period_days = models.PositiveIntegerField(default=0)
    trending_score = models.FloatField(default=0.0, db_index=True)
    deals_of_the_day = models.BooleanField(default=False)
    recommended_for_you = models.BooleanField(default=False)
    popular_product = models.BooleanField(default=False)
    delivery_options = models.ManyToManyField(DeliveryOption, through='ProductDeliveryOption', related_name='delivery_options')
    # Internal, platform-generated id. Was 4 digits (only 10,000 possible
    # values, so creates would start failing as the catalogue grew); new rows
    # get 10 digits. Existing short values stay valid.
    sku = ShortUUIDField(unique=True, length=10, max_length=20, prefix ="SKU", alphabet = "1234567890")
    # The seller's own stock code, used for their inventory and spreadsheet
    # updates. Unique within a store (see Meta.constraints), optional.
    seller_sku = models.CharField(max_length=64, blank=True, default='')
    # Why a product was rejected (or what to fix), written by the platform
    # reviewer and shown to the seller.
    review_note = models.TextField(blank=True, default='')
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(null=True, blank=True)
    views = models.PositiveIntegerField(default=0)
    search_vector = SearchVectorField(null=True, blank=True)
    avg_rating = models.FloatField(default=0.0, db_index=True)
    review_count = models.PositiveIntegerField(default=0)

    objects  = models.Manager() # Default Manager
    published = PublishedManager() # Custom Manager
    
    def save(self, *args, **kwargs):
        self.slug = slugify(self.title, allow_unicode=True)
        super(Product, self).save(*args, **kwargs)

        # Update search vector field in the database
        Product.objects.filter(pk=self.pk).update(
            search_vector=(
                SearchVector(F('title'), weight='A') +
                SearchVector(F('description'), weight='B') +
                SearchVector(F('features'), weight='C') +
                SearchVector(F('specifications'), weight='C')
            )
        )

    class Meta:
        ordering = ('-date',)
        indexes = [
            models.Index(fields=["sub_category", "status", "id"]),
            models.Index(fields=["sub_category", "status", "price"]),
            models.Index(fields=["sub_category", "status", "avg_rating"], name="product_cat_stat_avg_idx"),
            models.Index(fields=["sub_category", "status", "date"], name="product_cat_stat_date_idx"),
            models.Index(fields=["brand", "status"]),
            models.Index(fields=["brand", "status", "price"], name="product_brand_stat_price_idx"),
            models.Index(fields=["brand", "status", "avg_rating"], name="product_brand_stat_avg_idx"),
            models.Index(fields=["vendor", "status"]),
            models.Index(fields=["status"]),
            models.Index(fields=["status", "avg_rating"], name="product_stat_avg_rating_idx"),
            models.Index(fields=["status", "price"], name="product_stat_price_idx"),
            models.Index(fields=["product_type"]),
            models.Index(fields=["views"]),
            models.Index(fields=["date"]),
            GinIndex(fields=["search_vector"]),
        ]
        constraints = [
            # A seller's own SKU identifies one product within their store.
            models.UniqueConstraint(
                fields=["vendor", "seller_sku"],
                condition=~models.Q(seller_sku=""),
                name="uniq_product_seller_sku_per_vendor",
            ),
        ]

    def product_image(self):
        if self.image and hasattr(self.image, 'url'):  # check if image exists
            return mark_safe(f'<img src="{self.image.url}" width="50" height="50" />')
        return "No Image"
    
    def __str__(self):
        return self.title
    
    def get_percentage(self):
        """Calculate the discount percentage between old_price and price."""
        if not self.price or self.price == 0:
            return 0
        return (self.price - self.old_price) / self.price * 100
    
    def get_stock_quantity(self, variant=None):
        if self.variant in ['Size', 'Color', 'Size-Color']:
            if variant:
                return variant.quantity
            return self.variants.aggregate(total=Sum('quantity'))['total'] or 0
        return self.total_quantity
    
    @property
    def packaging_fee(self):
        return calculate_packaging_fee(self.weight, self.volume)


class ProductImages(models.Model):
    images = models.ImageField(upload_to="product_images/", default="product.jpg")
    product = models.ForeignKey(Product, related_name="p_images", on_delete=models.SET_NULL, null=True)
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-id',)
        verbose_name_plural = "Product Images"

################################### product review, whishlist, address #######################

class Color(models.Model):
    name = models.CharField(max_length=20)
    code = models.CharField(max_length=10, blank=True, null=True)

    def __str__(self):
        return self.name
    def color_tag(self):
        if self.code is not None:
            return mark_safe('<p style="background-color:{}">Color </p>'.format(self.code))
        else:
            return ""

class Size(models.Model):
    name = models.CharField(max_length=20)
    code = models.CharField(max_length=10, blank=True, null=True)

    def __str__(self):
        return self.name


    
class Variants(models.Model):
    title = models.CharField(max_length=225, blank=True, null=True)
    product = models.ForeignKey(Product, related_name="variants", on_delete=models.CASCADE)
    size = models.ForeignKey(Size, on_delete=models.SET_NULL, blank=True, null=True)
    color = models.ForeignKey(Color, on_delete=models.SET_NULL, blank=True, null=True)
    image = models.ImageField(upload_to="variants/", default="product.jpg")
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Seller's own code for this exact size/colour. Uniqueness within the
    # store is checked in the seller product serializer (it spans products).
    seller_sku = models.CharField(max_length=64, blank=True, default='')
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)


    class Meta:
        indexes = [
            models.Index(fields=['product', 'seller_sku']),
            models.Index(fields=['product', 'color']),
            models.Index(fields=['product', 'size']),
            models.Index(fields=['product', 'color', 'size']),
            models.Index(fields=['color']),
            models.Index(fields=['size']),
        ]

    def get_combined_title(self):
        """
        Combine the base title with size and color if available.
        """
        components = [self.product.title]  # use product title as base

        if self.size and self.size.name:
            components.append(self.size.name)

        if self.color and self.color.name:
            components.append(self.color.name)

        return " - ".join(components)

    def save(self, *args, **kwargs):
        # Automatically set the title before saving
        self.title = self.get_combined_title()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.get_combined_title()
    
    def product_image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.image.url))

class VariantImage(models.Model):
    variant = models.ForeignKey(Variants, on_delete=models.CASCADE, null=True)
    images = models.ImageField(upload_to="product_images/", default="product.jpg")
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.variant.title
    
    class Meta:
        ordering = ('-id',)
    
    def image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.images.url))


# FrequentlyBoughtTogether lived here — a table of apriori association rules
# rebuilt every six hours by product.tasks.generate_fbt. Superseded by
# recommendation.ProductNeighbor (kind='co_purchase'), which scores the same
# pairings as cosine-normalised co-occurrence rather than raw support, so the
# platform's best-seller stops appearing as a "complement" to every product.

from django.core.validators import MinValueValidator, MaxValueValidator
class ProductReview(models.Model):
    """
    A customer's review of a product they bought and received.

    Two independent checks (product/review_services.py):
      - purchase verification: derived from a delivered order line owned by the
        author (order_item, is_verified_purchase). Never taken from the client.
      - content moderation: automatic rules decide APPROVED or PENDING; staff
        approve, reject or hide. Only APPROVED reviews are public or counted.

    Policy: one review per customer per product (database constraint), whatever
    the number of purchases; the most recent delivered line is the evidence.
    """
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    HIDDEN = 'hidden'
    MODERATION_CHOICES = [
        (PENDING, 'Pending moderation'),
        (APPROVED, 'Approved'),
        (REJECTED, 'Rejected'),
        (HIDDEN, 'Hidden'),
    ]

    RATING = (
        (1, "★✰✰✰✰"),
        (2, "★★✰✰✰"),
        (3, "★★★✰✰"),
        (4, "★★★★✰"),
        (5, "★★★★★"),
    )
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name='reviews',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        related_name="reviews"
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        related_name='product_reviews',
        null=True,
        blank=True
    )
    # The purchase that verifies this review. Kept if the order is later
    # refunded: the customer did buy and receive the item.
    order_item = models.ForeignKey(
        'order.OrderProduct', on_delete=models.SET_NULL, null=True, blank=True, related_name='reviews',
    )
    title = models.CharField(max_length=120, blank=True, default='')
    review = models.TextField(max_length=1000, blank=False)
    rating = models.IntegerField(
        choices=RATING,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        blank=False
    )
    # Source of truth for visibility. Changed only by review_services.
    moderation_status = models.CharField(max_length=10, choices=MODERATION_CHOICES, default=PENDING,
                                         db_index=True)
    # Derived "is public" flag (= moderation_status == APPROVED), set in save().
    # Kept because many queries across the project filter on status=True.
    status = models.BooleanField(default=False)
    # Internal moderation record; never exposed publicly.
    moderation_flags = models.JSONField(default=list, blank=True,
                                        help_text="Automatic rules that sent this review to manual moderation.")
    moderation_reason = models.TextField(blank=True, default='')
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='+')
    # Normalised-text fingerprint for duplicate / copy-paste detection.
    content_hash = models.CharField(max_length=64, blank=True, default='', db_index=True)
    # Shopper-facing context (see product/review_views.py):
    # verified = written by someone with a delivered order line for this product;
    # purchased_variant = what they bought, e.g. "Size M · Black" (helps others choose).
    is_verified_purchase = models.BooleanField(default=False)
    purchased_variant = models.CharField(max_length=120, blank=True, default='')
    # Denormalised for sorting/filtering at scale (kept in sync by review_views
    # and the ReviewHelpfulVote/ReviewMedia writers).
    helpful_count = models.PositiveIntegerField(default=0)
    has_media = models.BooleanField(default=False)
    seller_reply = models.TextField(max_length=1000, blank=True, default='')
    seller_replied_at = models.DateTimeField(null=True, blank=True)
    seller_replied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Product Reviews"
        ordering = ['-date']
        permissions = [('moderate_productreview', 'Can moderate product reviews')]
        constraints = [
            # One review per customer per product. Hidden rows are excluded only
            # so pre-existing duplicates could be kept (hidden) by migration
            # 0018; the service layer still refuses any second review.
            models.UniqueConstraint(
                fields=['user', 'product'],
                condition=models.Q(user__isnull=False, product__isnull=False) & ~models.Q(moderation_status='hidden'),
                name='uniq_review_per_user_product',
            ),
        ]
        indexes = [
            models.Index(fields=['product', 'status']),
            models.Index(fields=['product', 'rating', 'status']),
            # Review list sorts / "with photos" filter on the product page.
            models.Index(fields=['product', 'status', '-helpful_count'], name='review_helpful_idx'),
            models.Index(fields=['product', 'status', 'has_media'], name='review_media_idx'),
        ]

    def __str__(self):
        return f"Review for {self.product.title if self.product else 'Deleted Product'} by {self.user.email if self.user else 'Anonymous'}"

    def get_rating(self):
        return dict(self.RATING).get(self.rating, "No rating")

    def rate_percentage(self):
        return (self.rating / 5) * 100

    def save(self, *args, **kwargs):
        if self.product and not self.vendor:
            self.vendor = self.product.vendor  # Automatically set vendor from product
        self.status = self.moderation_status == self.APPROVED
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'moderation_status' in update_fields:
            kwargs['update_fields'] = set(update_fields) | {'status'}
        super().save(*args, **kwargs)

    @property
    def is_public(self):
        return self.moderation_status == self.APPROVED


class ReviewModerationEvent(models.Model):
    """Audit trail: every status change of a review, by whom (NULL = automatic) and why."""
    review = models.ForeignKey(ProductReview, on_delete=models.CASCADE, related_name='moderation_events')
    from_status = models.CharField(max_length=10, blank=True, default='')
    to_status = models.CharField(max_length=10)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+')
    reason = models.TextField(blank=True, default='')
    flags = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['review', '-created_at'])]

    def __str__(self):
        return f"Review {self.review_id}: {self.from_status or '-'} → {self.to_status}"


def review_media_path(instance, filename):
    return f"reviews/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{os.path.splitext(filename)[1].lower()}"


class ReviewMedia(models.Model):
    """
    A photo or video attached to a review.

    Uploaded on its own before the review is posted (so each file gets its own
    progress bar and the review request stays small), then attached by id.
    Unattached uploads are deleted after a day (product.tasks.cleanup_orphan_review_media).
    Images are re-encoded on upload: EXIF (incl. GPS location) stripped,
    longest side capped, plus a small thumbnail for grids. See product/review_media.py.
    """
    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_CHOICES = [(KIND_IMAGE, 'Image'), (KIND_VIDEO, 'Video')]

    review = models.ForeignKey(ProductReview, on_delete=models.CASCADE, null=True, blank=True, related_name='media')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    file = models.FileField(upload_to=review_media_path, max_length=255)
    thumbnail = models.ImageField(upload_to=review_media_path, max_length=255, null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)
    position = models.PositiveSmallIntegerField(default=0)
    # Staff moderation: hidden media is not shown to shoppers.
    is_hidden = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position', 'id']
        indexes = [
            models.Index(fields=['review', 'is_hidden', 'position']),
            models.Index(fields=['uploaded_by', 'review', 'created_at']),
        ]
        verbose_name_plural = 'Review media'

    def __str__(self):
        return f"{self.kind} for review {self.review_id or '(unattached)'}"


class ReviewHelpfulVote(models.Model):
    """One shopper finding one review helpful. Count is mirrored on ProductReview.helpful_count."""
    review = models.ForeignKey(ProductReview, on_delete=models.CASCADE, related_name='helpful_votes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['review', 'user'], name='uniq_helpful_vote')]


class ReviewReport(models.Model):
    """
    A seller (or later a shopper) flags a review for Negromart staff, e.g. it
    is abusive, about a different product, or contains personal data. Staff
    decide whether to hide it; the reporter never can.
    """
    REASON_CHOICES = [
        ('abusive', 'Abusive or offensive'),
        ('not_about_product', 'Not about this product'),
        ('fake', 'Suspected fake review'),
        ('personal_info', 'Contains personal information'),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('upheld', 'Upheld (review hidden)'),
        ('dismissed', 'Dismissed'),
    ]

    review = models.ForeignKey(ProductReview, on_delete=models.CASCADE, related_name='reports')
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='review_reports')
    reason = models.CharField(max_length=30, choices=REASON_CHOICES)
    details = models.TextField(max_length=1000, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open', db_index=True)
    resolution_note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # One open report per review per store; re-reporting is noise.
            models.UniqueConstraint(
                fields=['review', 'vendor'], condition=models.Q(status='open'),
                name='uniq_open_review_report_per_vendor',
            ),
        ]

    def __str__(self):
        return f"Report on review {self.review_id} ({self.get_reason_display()})"


class Wishlist(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='wishlists', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, related_name='whishlist', on_delete=models.CASCADE)
    saved_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "wishlists"
        unique_together = ('user', 'product')

    def __str__(self):
        return self.product.title

class SavedProduct(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='saved_products', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    variant = models.ForeignKey(Variants, on_delete=models.SET_NULL, null=True, blank=True)
    saved_date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.product.title}"


class ProductDeliveryOption(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    variant = models.ForeignKey(Variants, related_name='delivery_options', on_delete=models.CASCADE, null=True, blank=True)
    delivery_option = models.ForeignKey(DeliveryOption, on_delete=models.PROTECT)
    default = models.BooleanField(default=False)
    added_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.delivery_option.name

    def get_delivery_date_range(self, reference_date=None, buyer_country=None, dynamic_min_days=None, dynamic_max_days=None):
        """
        Get the delivery date range for this product, delegating to DeliveryOption.
        Supports buyer_country for international checks and dynamic overrides from DHL API.
        """
        if not self.delivery_option:
            logger.warning(f"No delivery option set for ProductDeliveryOption (product: {self.product.title})")
            return None

        # Fix: Use shipping_from_country (not vendor.country)
        vendor_country = self.product.vendor.shipping_from_country.code if self.product.vendor.shipping_from_country else 'GH'
        is_international = buyer_country and buyer_country != vendor_country

        return self.delivery_option.get_delivery_date_range(
            reference_date, dynamic_min_days=dynamic_min_days, dynamic_max_days=dynamic_max_days
        )
    
class Coupon(models.Model):
    code = models.CharField(max_length=50, unique=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2)
    discount_percentage = models.FloatField(null=True, blank=True)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField()
    active = models.BooleanField(default=True)
    max_uses = models.IntegerField(null=True, blank=True)
    used_count = models.IntegerField(default=0)
    min_purchase_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    applicable_products = models.ManyToManyField(Product, blank=True)
    applicable_categories = models.ManyToManyField(Category, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.code

    def is_valid(self):
        now = timezone.now()
        return self.active and self.valid_from <= now <= self.valid_to and (self.max_uses is None or self.used_count < self.max_uses)

class ClippedCoupon(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='clipped_coupons', on_delete=models.CASCADE)
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE)
    clipped_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} clipped {self.coupon.code}"


####################### FLASH SALE MODEL ##################

class FlashSale(models.Model):
    """
    Time-limited discount event tied to a product (or specific variant).
    sale_price and original_price are stored denormalized so the deal card
    stays accurate even after the product price changes.
    """

    LABEL_CHOICES = [
        ('lightning', 'Lightning Deal'),
        ('limited',   'Limited Offer'),
        ('clearance', 'Clearance'),
        ('daily',     'Daily Deal'),
    ]

    product       = models.ForeignKey(Product,  on_delete=models.SET_NULL, null=True,  related_name='flash_sales')
    variant       = models.ForeignKey(Variants, on_delete=models.SET_NULL, null=True, blank=True, related_name='flash_sales')
    sale_price    = models.DecimalField(max_digits=10, decimal_places=2)
    original_price = models.DecimalField(max_digits=10, decimal_places=2)
    start_time    = models.DateTimeField(db_index=True)
    end_time      = models.DateTimeField(db_index=True)
    max_quantity  = models.PositiveIntegerField(null=True, blank=True, help_text="Cap on units sold at flash price. Leave blank for unlimited.")
    sold_count    = models.PositiveIntegerField(default=0)
    label         = models.CharField(max_length=20, choices=LABEL_CHOICES, default='lightning')
    is_active     = models.BooleanField(default=True, db_index=True)
    created_by    = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='flash_sales')
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['end_time']
        indexes = [
            models.Index(fields=['is_active', 'start_time', 'end_time']),
        ]

    def __str__(self):
        product_title = self.product.title if self.product else "Deleted Product"
        return f"{self.get_label_display()} — {product_title}"

    @property
    def discount_percentage(self):
        if not self.original_price or self.original_price == 0:
            return 0
        return round((1 - self.sale_price / self.original_price) * 100, 1)

    @property
    def is_live(self):
        if not self.start_time or not self.end_time:
            return False
        now = timezone.now()
        return self.is_active and self.start_time <= now <= self.end_time

    @property
    def stock_remaining(self):
        if self.max_quantity is None:
            return None
        return max(self.max_quantity - self.sold_count, 0)

    @property
    def stock_percentage(self):
        if not self.max_quantity:
            return 100
        return round((self.stock_remaining / self.max_quantity) * 100, 1)

    @property
    def seconds_remaining(self):
        if not self.end_time:
            return 0
        now = timezone.now()
        if now >= self.end_time:
            return 0
        return int((self.end_time - now).total_seconds())


class Collection(models.Model):
    """
    Reusable marketing collection — Back to School, Christmas, Valentine's, etc.
    Can be populated manually (hand-pick products) or automatically from a
    sub-category, so the same URL structure works for any campaign.
    """
    FILTER_TYPE_CHOICES = [
        ('manual',       'Manual – hand-picked products'),
        ('sub_category', 'Sub-category – all products in a sub-category'),
        ('flash_sale',   'Flash Sale – all live flash-sale products'),
    ]

    slug          = models.SlugField(unique=True)
    title         = models.CharField(max_length=200)
    subtitle      = models.CharField(max_length=300, blank=True)
    description   = models.TextField(blank=True)
    banner_image  = models.ImageField(upload_to='collections/', null=True, blank=True)
    accent_color  = models.CharField(max_length=7, default='#212121', help_text='Hex color, e.g. #E53935')
    icon          = models.CharField(max_length=50, blank=True, help_text='Lucide icon name shown in the header, e.g. Zap, ShoppingBag, Star')

    filter_type   = models.CharField(max_length=20, choices=FILTER_TYPE_CHOICES, default='manual')
    sub_category  = models.ForeignKey('Sub_Category', null=True, blank=True, on_delete=models.SET_NULL, related_name='collections')
    products      = models.ManyToManyField('Product', blank=True, related_name='collections')

    is_active     = models.BooleanField(default=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    def get_products_qs(self):
        if self.filter_type == 'sub_category' and self.sub_category:
            return Product.published.filter(sub_category=self.sub_category)
        if self.filter_type == 'flash_sale':
            now = timezone.now()
            ids = FlashSale.objects.filter(
                is_active=True, start_time__lte=now, end_time__gte=now
            ).values_list('product_id', flat=True)
            return Product.published.filter(id__in=ids)
        return self.products.filter(status='published', vendor__shop_paused=False, vendor__is_suspended=False)


############################################################
####################### OCCASIONS MODEL ####################
############################################################

class Occasion(models.Model):
    """
    A seasonal/holiday marketing hub (Mother's Day, Christmas, etc.).
    Shows automatically within start_date → end_date; leave blank to
    control visibility manually with is_active.
    """
    title        = models.CharField(max_length=200)
    slug         = models.SlugField(unique=True)
    subtitle     = models.CharField(max_length=300, blank=True,
                                    help_text="Bottom tag-line, e.g. 'Get it all right here'")
    icon         = models.CharField(max_length=10, blank=True,
                                    help_text="Optional emoji shown beside the title, e.g. 🌸")
    accent_color = models.CharField(max_length=7, default='#0071CE',
                                    help_text="Hex color used for hover/accent elements")
    is_active    = models.BooleanField(default=True)
    start_date   = models.DateField(null=True, blank=True,
                                    help_text="Auto-show from this date (leave blank = always active)")
    end_date     = models.DateField(null=True, blank=True,
                                    help_text="Auto-hide after this date (leave blank = no expiry)")
    position     = models.PositiveIntegerField(default=0, help_text="Lower = shown first on homepage")
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position']
        verbose_name = 'Occasion'
        verbose_name_plural = 'Occasions'

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)


class OccasionSection(models.Model):
    """
    A named sub-group inside an Occasion (e.g. "Everything Mom wants").
    Each section links to a Collection, which drives the product preview and
    the "View all" destination page (/collection/<slug>).
    """
    occasion   = models.ForeignKey(Occasion, on_delete=models.CASCADE, related_name='sections')
    title      = models.CharField(max_length=200,
                                  help_text="Section heading, e.g. 'Everything Mom wants'")
    collection = models.ForeignKey(
        Collection, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='occasion_sections',
        help_text="Products shown in this card and destination of the 'View all' link"
    )
    position   = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['position']
        verbose_name = 'Occasion Section'
        verbose_name_plural = 'Occasion Sections'

    def __str__(self):
        return f"{self.occasion.title} — {self.title}"


# ── View Analytics ────────────────────────────────────────────────────────────

class ProductViewLog(models.Model):
    """
    One row per deduplicated view event, written async via Celery.
    Drives the per-product time-series analytics in the seller dashboard.
    Bot views are stored but excluded from product.views total.
    """
    DEVICE_CHOICES = [
        ('mobile',  'Mobile'),
        ('tablet',  'Tablet'),
        ('desktop', 'Desktop'),
        ('unknown', 'Unknown'),
    ]

    product      = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='view_logs')
    visitor_key  = models.CharField(max_length=100)   # "u:{id}" or "v:{uuid}"
    user         = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    is_bot       = models.BooleanField(default=False)
    is_returning = models.BooleanField(default=False)  # True if visitor came back within 30-day window
    device_type  = models.CharField(max_length=10, choices=DEVICE_CHOICES, default='unknown')
    date         = models.DateField()                  # denormalized for fast date-range queries
    viewed_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['product', 'date']),
            models.Index(fields=['product', 'is_bot', 'date']),
            models.Index(fields=['visitor_key', 'product']),
        ]
        ordering = ['-viewed_at']

    def __str__(self):
        return f"{self.product_id} viewed by {self.visitor_key} on {self.date}"


class ProductDailyStats(models.Model):
    """
    Daily aggregate per product, materialized by Celery at midnight UTC.
    Provides fast seller-dashboard queries without scanning ProductViewLog.
    """
    product          = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='daily_stats')
    date             = models.DateField()
    total_views      = models.PositiveIntegerField(default=0)
    unique_views     = models.PositiveIntegerField(default=0)
    returning_views  = models.PositiveIntegerField(default=0)
    bot_views        = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('product', 'date')
        indexes = [models.Index(fields=['product', 'date'])]

    def __str__(self):
        return f"{self.product_id} stats for {self.date}"


class RecentlyViewedProduct(models.Model):
    """
    DB-backed recently viewed for authenticated users.
    Enables cross-device sync after a guest → logged-in transition.
    upsert logic: if the row exists, update viewed_at; otherwise insert.
    """
    user       = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recently_viewed'
    )
    product    = models.ForeignKey('Product', on_delete=models.CASCADE)
    viewed_at  = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'product')
        indexes = [models.Index(fields=['user', 'viewed_at'])]
        ordering = ['-viewed_at']

    def __str__(self):
        return f"{self.user_id} → {self.product_id}"
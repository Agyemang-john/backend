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
# from order.models import DeliveryType
# Create your models here.
from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.search import SearchVector
from django.db.models import F, Sum
   
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
        return super().get_queryset().filter(status='published')

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

class ProductView(models.Model):
    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name='product_views')
    device_id = models.CharField(max_length=36)  # For UUID storage
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('product', 'device_id')
        indexes = [
            models.Index(fields=['product', 'device_id']),
            models.Index(fields=['created_at']),
        ]

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
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, null=True, related_name="product")
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
    sku = ShortUUIDField(unique=True, length=4, max_length=10, prefix ="SKU", alphabet = "1234567890")
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(null=True, blank=True)
    views = models.PositiveIntegerField(default=0)
    search_vector = SearchVectorField(null=True, blank=True)

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
        verbose_name_plural = "products"
        indexes = [
            GinIndex(fields=["search_vector"]),
        ]

    def product_image(self):
        if self.image and hasattr(self.image, 'url'):  # check if image exists
            return mark_safe(f'<img src="{self.image.url}" width="50" height="50" />')
        return "No Image"
    
    def __str__(self):
        return self.title
    
    def get_percentage(self):
        new_price = (self.price - self.old_price) / (self.price) * 100
        return new_price
    
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
    size = models.ForeignKey(Size, on_delete=models.CASCADE, blank=True, null=True)
    color = models.ForeignKey(Color, on_delete=models.CASCADE, blank=True, null=True)
    image = models.ImageField(upload_to="variants/", default="product.jpg")
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

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


class FrequentlyBoughtTogether(models.Model):
    product = models.ForeignKey(Product, related_name='frequently_bought_with', on_delete=models.CASCADE)
    recommended = models.ForeignKey(Product, related_name='+', on_delete=models.CASCADE)

    class Meta:
        unique_together = ('product', 'recommended')

from django.core.validators import MinValueValidator, MaxValueValidator
class ProductReview(models.Model):
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
        on_delete=models.CASCADE,
        related_name='product_reviews',
        null=True
    )
    review = models.TextField(max_length=1000, blank=False)
    rating = models.IntegerField(
        choices=RATING,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        blank=False
    )
    status = models.BooleanField(default=False)  # True = Published, False = Hidden
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Product Reviews"
        ordering = ['-date']

    def __str__(self):
        return f"Review for {self.product.title if self.product else 'Deleted Product'} by {self.user.email if self.user else 'Anonymous'}"

    def get_rating(self):
        return dict(self.RATING).get(self.rating, "No rating")

    def rate_percentage(self):
        return (self.rating / 5) * 100

    def save(self, *args, **kwargs):
        if self.product and not self.vendor:
            self.vendor = self.product.vendor  # Automatically set vendor from product
        super().save(*args, **kwargs)
    

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
    delivery_option = models.ForeignKey(DeliveryOption, on_delete=models.CASCADE)
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
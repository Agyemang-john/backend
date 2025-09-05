from django.db import models
from shortuuid.django_fields import ShortUUIDField
from django.utils.html import mark_safe
from userauths.models import User
from taggit.managers import TaggableManager
from django.urls import reverse
from django.utils.text import slugify
from vendor.models import *
from core.models import *
from .utils import *
from datetime import datetime, timedelta
from address.models import Region
from django_ckeditor_5.fields import CKEditor5Field

# from order.models import DeliveryType
# Create your models here.
from django.conf import settings
   
############################################################
####################### CATEGORIES MODEL ##################


class Main_Category(models.Model):
    title = models.CharField(max_length=100, unique=True, default="Food")
    slug = models.SlugField(max_length=100, unique=True)

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
    name = models.CharField(max_length=100)
    description = models.TextField()
    min_days = models.IntegerField(default=0)
    max_days = models.IntegerField(default=0)
    cost = models.DecimalField(max_digits=10, decimal_places=2)

    def get_delivery_date_range(self):
        now = datetime.now()
        if self.name.lower() in ["same-day delivery", "same-day"]:
            if now.hour >= 10:
                return "Tomorrow"
            else:
                return "Today"

        min_delivery_date = now + timedelta(days=self.min_days)
        max_delivery_date = now + timedelta(days=self.max_days)
        return min_delivery_date, max_delivery_date

    def get_delivery_status(self):
        now = datetime.now().date()
        delivery_range = self.get_delivery_date_range()

        if isinstance(delivery_range, str):  # "Today" or "Tomorrow"
            return delivery_range

        min_date, max_date = delivery_range
        min_date = min_date.date()
        max_date = max_date.date()

        if max_date < now:
            return "OVER"
        elif min_date > now:
            days_until_start = (min_date - now).days
            return "TOMORROW" if days_until_start == 1 else f"IN {days_until_start} DAYS"
        elif min_date <= now <= max_date:
            if min_date == max_date == now:
                return "TODAY"
            return "ONGOING"

        return "PAST"

    def __str__(self):
        return self.name


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
    image = models.ImageField(upload_to=vendor_directory_path, default="product.jpg", help_text="Main image of the product")
    video = models.FileField(upload_to="video/%y", default="video.mp4")
    price = models.FloatField(default="1.99", help_text="Base currency in GHS (e.g 70)")
    old_price = models.FloatField(default="2.99", help_text="Base currency in GHS (e.g 50)")
    features = CKEditor5Field(null=True, blank=True, default="Black")
    description = CKEditor5Field(null=True, blank=True, default="I sell good products only")
    specifications = CKEditor5Field(null=True, blank=True, default="Black")
    delivery_returns = CKEditor5Field(null=True, blank=True, default="We offer free standard shipping on all orders")
    available_in_regions = models.ManyToManyField(Region, blank=True, related_name='products')
    product_type = models.CharField(max_length=50, choices=OPTIONS, null=True, blank=True, default='new')
    total_quantity = models.IntegerField(default="100", null=True, blank=True)
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
    delivery_options = models.ManyToManyField(DeliveryOption, through='ProductDeliveryOption')
    sku = ShortUUIDField(unique=True, length=4, max_length=10, prefix ="SKU", alphabet = "1234567890")
    date = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(null=True, blank=True)
    views = models.PositiveIntegerField(default=0)

    objects  = models.Manager() # Default Manager
    published = PublishedManager() # Custom Manager

    def get_absolute_url(self):
        return reverse("product:product_detail", args=[self.sub_category.slug, self.id, self.slug])
    
    def save(self, *args, **kwargs):
        self.slug = slugify(self.title, allow_unicode=True)
        super(Product, self).save(*args, **kwargs)

    class Meta:
        verbose_name_plural = "products"

    def product_image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.image.url))
    
    def __str__(self):
        return self.title
    
    def get_percentage(self):
        new_price = (self.price - self.old_price) / (self.price) * 100
        return new_price
    
    @property
    def packaging_fee(self):
        return calculate_packaging_fee(self.weight, self.volume)




class ProductImages(models.Model):
    title = models.CharField(max_length=200, null=True, blank=True)
    images = models.ImageField(upload_to="product_images/", default="product.jpg")
    product = models.ForeignKey(Product, related_name="p_images", on_delete=models.SET_NULL, null=True)
    date = models.DateTimeField(auto_now_add=True)


    class Meta:
        ordering = ('-id',)
        verbose_name_plural = "Product Images"

################################### product review, whishlist, address #######################
################################### product review, whishlist, address #######################
################################### product review, whishlist, address #######################
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
    title = models.CharField(max_length=50)
    product = models.ForeignKey(Product, related_name="variants", on_delete=models.CASCADE)
    size = models.ForeignKey(Size, on_delete=models.CASCADE,blank=True, null=True)
    color = models.ForeignKey(Color, on_delete=models.CASCADE,blank=True, null=True)
    image = models.ImageField(upload_to="variants/", default="product.jpg")
    quantity = models.IntegerField(default=1)
    price = models.FloatField(default=0)
    sku = ShortUUIDField(unique=True, length=30, max_length=40, prefix ="SKU", alphabet = "1234567890")
    url = models.CharField(max_length=200, null=True, blank=True)

    def get_combined_title(self):
        """
        Combine the names of size and color with the title.
        """
        components = [self.title]  # Always include the base title

        # Add size name if available
        if self.size and self.size.name:
            components.append(self.size.name)

        # Add color name if available
        if self.color and self.color.name:
            components.append(self.color.name)

        # Join components with a separator (e.g., " - ")
        return " - ".join(components)

    def __str__(self):
        return self.get_combined_title()
    
    def product_image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.image.url))




class VariantImage(models.Model):
    variant = models.ForeignKey(Variants, on_delete=models.CASCADE, null=True)
    images = models.ImageField(upload_to="product_images/", default="product.jpg")
    
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

class ProductReview(models.Model):
    RATING = (
        (1, "★✰✰✰✰"),
        (2, "★★✰✰✰"),
        (3, "★★★✰✰"),
        (4,"★★★★✰"),
        (5,"★★★★★"),
    )
    
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, related_name="reviews")
    review = models.TextField()
    rating = models.IntegerField(choices=RATING, default=None)
    status = models.BooleanField(default=False)
    date = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Product Reviews"

    def __str__(self):
        return self.product.title
    
    def get_rating(self):
        return self.rating
    
    def rate_percentage(self):
        percentage = (self.rating / 5) * 100
        return percentage
    

class Wishlist(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, related_name='whishlist', on_delete=models.CASCADE)
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "wishlists"
        unique_together = ('user', 'product')

    def __str__(self):
        return self.product.title

class SavedProduct(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
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

    def __str__(self):
        return self.delivery_option.name
    
    def get_delivery_date_range(self):
        now = datetime.now()
        if self.delivery_option.name.lower() in ["same-day delivery", "same-day"]:
            if now.hour >= 8:
                return "Tomorrow"
            else:
                return "Today"

        min_delivery_date = now + timedelta(days=self.delivery_option.min_days)
        max_delivery_date = now + timedelta(days=self.delivery_option.max_days)
        return min_delivery_date, max_delivery_date
    
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

    def __str__(self):
        return self.code

    def is_valid(self):
        now = timezone.now()
        return self.active and self.valid_from <= now <= self.valid_to and (self.max_uses is None or self.used_count < self.max_uses)

class ClippedCoupon(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE)
    clipped_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} clipped {self.coupon.code}"
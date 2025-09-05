from django.db import models

# Create your models here.
from django.db import models
from shortuuid.django_fields import ShortUUIDField
from django.utils.html import mark_safe
from userauths.models import User
from taggit.managers import TaggableManager
from django.urls import reverse
from django.utils.text import slugify
from django_countries.fields import CountryField
from vendor.models import *
from product.models import *
import math

class CurrencyRate(models.Model):
    currency = models.CharField(max_length=3, unique=True)
    rate = models.FloatField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.currency} - {self.rate}"

STATUS_CHOICE = (
    ("processing", "Processing"),
    ("delivered", "Delivered"),
    ("shipped", "Shipped"),
)

STATUS = (
    ("draft", "Draft"),
    ("disabled", "Disabled"),
    ("rejected", "Rejected"),
    ("in_review", "In Review"),
    ("published", "Published"),
)

RATING = (
    (1, "★✰✰✰✰"),
    (2, "★★✰✰✰"),
    (3, "★★★✰✰"),
    (4,"★★★★✰"),
    (5,"★★★★★"),
)


def user_directory_path(instance, filename):
    return 'user_{0}/{1}'.format(instance.user.id, filename)

# Create your models here.

############################################################
####################### MAIN SLIDER MODEL ##################
############################################################
    
class HomeSlider(models.Model):
    DEAL_TYPES = [
        ('Daily Deal', 'Daily Deal'),
        ('Discount', 'Discount'),
        ('Limited Time', 'Limited Time'),
        ('Featured', 'Featured'),
        ('Custom', 'Custom'),
    ]

    title = models.CharField(max_length=100, help_text="Main headline (e.g. Earphones)")
    description = models.TextField(blank=True, help_text="Short description of the banner")
    deal_type = models.CharField(max_length=20, choices=DEAL_TYPES, default='custom')

    # For price display
    price_prefix = models.CharField(max_length=50, blank=True, help_text="Text like 'Today:'")
    price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, help_text="Price (e.g. 247.99)")

    image_desktop = models.ImageField(upload_to='sliders/desktop/', blank=True, null=True)
    image_mobile = models.ImageField(upload_to='sliders/mobile/', blank=True, null=True)

    link_url = models.URLField(blank=True, help_text="Optional full link to a product, category, or page")

    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0, help_text="Slider order/priority")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']

    def slider_image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.image_desktop.url))

    def __str__(self):
        return self.title

    

############################################################
####################### MAIN BANNERS MODEL ##################
############################################################

class Banners(models.Model):
    DEAL_TYPES = [
        ('Daily Deal', 'Daily Deal'),
        ('Discount', 'Discount'),
        ('Limited Time', 'Limited Time'),
        ('Featured', 'Featured'),
        ('Custom', 'Custom'),
    ]
    image = models.ImageField(upload_to='banners', blank=True, null=True)
    deal_type = models.CharField(max_length=20, choices=DEAL_TYPES, default='custom')
    link = models.CharField(max_length=200)
    title = models.CharField(max_length=100, unique=True, default="Food")

    def banner_image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.image.url))


    def __str__(self):
        return self.title

############################################################
####################### IMAGE MODEL ##################
############################################################




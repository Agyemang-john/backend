"""
core/models.py
Models for homepage and promotional content:
- CurrencyRate: stores exchange rates for offline/fallback currency conversion
- HomeSlider: hero banner slides on the homepage
- Banners: smaller promotional banners throughout the site
"""

from django.db import models
from django.utils.html import mark_safe
from django.conf import settings
from django.utils import timezone
from vendor.models import *
from product.models import *

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

    TEXT_THEME_CHOICES = [
        ('light', 'Light (white text — for dark backgrounds)'),
        ('dark', 'Dark (black text — for light/white backgrounds)'),
    ]

    CONTENT_ALIGN_CHOICES = [
        ('left', 'Left'),
        ('center', 'Center'),
        ('right', 'Right'),
    ]

    title = models.CharField(max_length=100, help_text="Main headline (e.g. Earphones)")
    subtitle = models.CharField(
        max_length=200, blank=True,
        help_text="Smaller text shown above the title (e.g. 'Host an epic night in')"
    )
    description = models.TextField(blank=True, help_text="Short description of the banner")
    deal_type = models.CharField(max_length=20, choices=DEAL_TYPES, default='Custom')

    # For price display
    price_prefix = models.CharField(max_length=50, blank=True, help_text="Text like 'Today:'")
    price = models.DecimalField(
        max_digits=10, decimal_places=2, blank=True, null=True,
        help_text="Price (e.g. 247.99)"
    )

    image_desktop = models.ImageField(
        upload_to='sliders/desktop/', blank=True, null=True,
        help_text="Clean background/product image — NO text baked in. Recommended: 1240×380px"
    )
    image_mobile = models.ImageField(
        upload_to='sliders/mobile/', blank=True, null=True,
        help_text="Clean background/product image — NO text baked in. Recommended: 800×220px"
    )

    link_url = models.URLField(blank=True, help_text="Destination when user clicks anywhere on the slide or 'Shop now'")
    cta_label = models.CharField(
        max_length=50, default="Shop now",
        help_text="Call-to-action button text (e.g. 'Shop now', 'Try Walmart+ now')"
    )

    # Visual theme controls
    text_theme = models.CharField(
        max_length=5, choices=TEXT_THEME_CHOICES, default='light',
        help_text="Use 'dark' when your image has a white/light background"
    )
    content_align = models.CharField(
        max_length=6, choices=CONTENT_ALIGN_CHOICES, default='left',
        help_text="Which side of the slide the text block appears on"
    )

    # Dynamic CTA button position
    # Store as JSON: {"top": "12%", "left": "62%"} or {"bottom": "20%", "right": "8%"}
    # Leave null to use the default inline position inside the text block
    cta_position = models.JSONField(
        null=True, blank=True,
        help_text=(
            'Optional: float the "Shop now" button at a specific spot on the image. '
            'Enter as JSON with % values, e.g. {"top": "12%", "left": "62%"}. '
            'Leave blank to show the button inside the text block.'
        )
    )

    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0, help_text="Slider order/priority")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["order"]),
        ]

    def slider_image(self):
        """Admin thumbnail preview. Returns empty string if no image uploaded."""
        if self.image_desktop:
            return mark_safe('<img src="%s" width="50" height="50" />' % (self.image_desktop.url))
        return ""

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

    TEXT_THEME_CHOICES = [
        ('light', 'Light (white text — for dark backgrounds)'),
        ('dark', 'Dark (black text — for light/white backgrounds)'),
    ]

    CONTENT_ALIGN_CHOICES = [
        ('left', 'Left'),
        ('center', 'Center'),
        ('right', 'Right'),
    ]

    title = models.CharField(max_length=100, unique=True, default="Food")
    subtitle = models.CharField(
        max_length=200, blank=True,
        help_text="Smaller text shown above the title"
    )
    image = models.ImageField(
        upload_to='banners/', blank=True, null=True,
        help_text="Clean background/product image — NO text baked in. Recommended: 340×185px"
    )
    deal_type = models.CharField(max_length=20, choices=DEAL_TYPES, default='Custom')
    link = models.CharField(max_length=200)
    cta_label = models.CharField(
        max_length=50, default="Shop now",
        help_text="Call-to-action button text"
    )
    text_theme = models.CharField(
        max_length=5, choices=TEXT_THEME_CHOICES, default='light',
        help_text="Use 'dark' when your banner has a white/light background"
    )
    content_align = models.CharField(
        max_length=6, choices=CONTENT_ALIGN_CHOICES, default='left',
        help_text="Which side of the banner the text block appears on"
    )

    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']
        indexes = [
            models.Index(fields=['is_active']),
            models.Index(fields=['order']),
        ]

    def banner_image(self):
        """Admin thumbnail preview. Returns empty string if no image uploaded."""
        if self.image:
            return mark_safe('<img src="%s" width="50" height="50" />' % (self.image.url))
        return ""

    def __str__(self):
        return self.title
############################################################
####################### PROMO GRID ##################
############################################################

class PromoCard(models.Model):
    COLOR_CHOICES = [
        ('white',  'White'),
        ('yellow', 'Yellow'),
        ('blue',   'Blue'),
        ('green',  'Green'),
        ('coral',  'Coral'),
        ('dark',   'Dark'),
        ('gray',   'Gray'),
    ]
    BADGE_COLOR_CHOICES = [
        ('yellow', 'Yellow'),
        ('red',    'Red'),
        ('blue',   'Blue'),
        ('dark',   'Dark'),
    ]

    title       = models.CharField(max_length=120)
    eyebrow     = models.CharField(max_length=60, blank=True, help_text="Small label above the title")
    link_url    = models.CharField(max_length=300, help_text="e.g. /category/shoes or /flash-sales")
    link_text   = models.CharField(max_length=50, default="Shop now")
    image       = models.ImageField(upload_to='promo_cards/', null=True, blank=True)
    card_color  = models.CharField(max_length=10, choices=COLOR_CHOICES, default='white')
    badge_text  = models.CharField(max_length=40, blank=True, help_text="e.g. Flash Deals, New, Hot")
    badge_color = models.CharField(max_length=10, choices=BADGE_COLOR_CHOICES, default='yellow')
    text_color  = models.CharField(
        max_length=20, default='#ffffff',
        help_text="CSS color for title/eyebrow/link text (e.g. #ffffff or #1A1A1A)"
    )
    link_color  = models.CharField(
        max_length=20, default='#ffffff',
        help_text="CSS color for the 'Shop now' link text (e.g. #FFC220 or #0071CE)"
    )
    is_tall     = models.BooleanField(default=False, help_text="Spans 2 rows — use for the big right-column card")
    position    = models.PositiveIntegerField(default=0, help_text="Lower = shown first")
    is_active   = models.BooleanField(default=True)

    class Meta:
        ordering = ['position']
        verbose_name = 'Promo Card'
        verbose_name_plural = 'Promo Cards'

    def __str__(self):
        return self.title


############################################################
####################### SEARCH HISTORY ##################
############################################################

class SearchHistory(models.Model):
    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='search_history')
    query       = models.CharField(max_length=200)
    searched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-searched_at']
        unique_together = ('user', 'query')
        indexes = [models.Index(fields=['user', 'searched_at'])]

    def __str__(self):
        return f"{self.user_id} — {self.query}"


############################################################
####################### IMAGE MODEL ##################
############################################################


############################################################
##################### COMMUNITY / SOCIAL LINKS ############
############################################################

PLATFORM_CHOICES = [
    ('whatsapp',  'WhatsApp'),
    ('telegram',  'Telegram'),
    ('facebook',  'Facebook'),
    ('instagram', 'Instagram'),
    ('twitter',   'Twitter / X'),
    ('youtube',   'YouTube'),
    ('linkedin',  'LinkedIn'),
    ('tiktok',    'TikTok'),
    ('discord',   'Discord'),
    ('pinterest', 'Pinterest'),
    ('snapchat',  'Snapchat'),
    ('other',     'Other'),
]

LINK_CATEGORY_CHOICES = [
    ('community', 'Community Group'),   # WhatsApp channel, Telegram group, Discord, Facebook group
    ('social',    'Social Media'),      # Instagram, YouTube, LinkedIn, TikTok, Twitter
]


class SocialLink(models.Model):
    platform     = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    category     = models.CharField(
        max_length=10, choices=LINK_CATEGORY_CHOICES, default='social',
        help_text="'Community' = interactive groups (WhatsApp/Telegram/Discord); 'Social' = follow pages (Instagram/YouTube/etc.)"
    )
    label        = models.CharField(max_length=100, help_text="Display name on the card, e.g. 'WhatsApp Channel'")
    url          = models.URLField(max_length=500)
    description  = models.CharField(max_length=200, blank=True, help_text="Short tagline, e.g. 'Get exclusive deals first'")
    member_count = models.CharField(max_length=40, blank=True, help_text="e.g. '12K+ members' — leave blank to hide")
    is_active    = models.BooleanField(default=True)
    order        = models.PositiveIntegerField(default=0, help_text="Lower = shown first within the category")
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'order', 'platform']
        indexes = [
            models.Index(fields=['is_active', 'category']),
            models.Index(fields=['order']),
        ]
        verbose_name = 'Social / Community Link'
        verbose_name_plural = 'Social / Community Links'

    def __str__(self):
        return f"{self.get_platform_display()} — {self.label}"

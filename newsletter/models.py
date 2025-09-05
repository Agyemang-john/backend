from django.db import models
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.core.validators import validate_email

class Tag(models.Model):
    """Simple segmentation via tags (e.g., 'buyers', 'vendors', 'ghana')."""
    name = models.CharField(max_length=64, unique=True, help_text="(e.g., 'buyers', 'vendors', 'ghana').")

    def __str__(self):
        return self.name

class Subscriber(models.Model):
    email = models.EmailField(unique=True, validators=[validate_email])
    first_name = models.CharField(max_length=80, blank=True)
    last_name  = models.CharField(max_length=80, blank=True)
    locale     = models.CharField(max_length=12, blank=True)   # e.g. 'en', 'fr'
    country    = models.CharField(max_length=64, blank=True)
    tags       = models.ManyToManyField(Tag, blank=True)

    is_active      = models.BooleanField(default=False)  # becomes True after confirm
    confirmed_at   = models.DateTimeField(null=True, blank=True)
    unsubscribed_at= models.DateTimeField(null=True, blank=True)
    bounce_count   = models.PositiveIntegerField(default=0)

    # tokens for confirm/unsubscribe links
    confirm_token     = models.CharField(max_length=64, default="", blank=True)
    unsubscribe_token = models.CharField(max_length=64, default="", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def generate_tokens(self, save=True):
        self.confirm_token = get_random_string(40)
        self.unsubscribe_token = get_random_string(40)
        if save:
            self.save(update_fields=["confirm_token", "unsubscribe_token"])

    def mark_confirmed(self):
        self.is_active = True
        self.confirmed_at = timezone.now()
        self.save(update_fields=["is_active", "confirmed_at"])

    def mark_unsubscribed(self):
        self.is_active = False
        self.unsubscribed_at = timezone.now()
        self.save(update_fields=["is_active", "unsubscribed_at"])

    def __str__(self):
        return self.email


class NewsletterTemplate(models.Model):
    """Reusable HTML templates with placeholders like {{ first_name }}."""
    name = models.CharField(max_length=120, unique=True)
    subject_template = models.CharField(max_length=200)
    html_template    = models.TextField()  # store full HTML; render with Django template engine
    text_template    = models.TextField(blank=True, help_text="Optional plain-text fallback")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Campaign(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("scheduled", "Scheduled"),
        ("sending", "Sending"),
        ("sent", "Sent"),
        ("cancelled", "Cancelled"),
    ]

    name        = models.CharField(max_length=160, unique=True)
    template    = models.ForeignKey(NewsletterTemplate, on_delete=models.PROTECT)
    from_email  = models.EmailField()
    preheader   = models.CharField(max_length=180, blank=True)
    # Simple segmentation: choose tags to include/exclude
    include_tags = models.ManyToManyField(Tag, blank=True, related_name="include_in_campaigns")
    exclude_tags = models.ManyToManyField(Tag, blank=True, related_name="exclude_from_campaigns")

    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at   = models.DateTimeField(null=True, blank=True)
    finished_at  = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="draft")

    batch_size      = models.PositiveIntegerField(default=500)   # per Celery task batch
    throttle_per_min= models.PositiveIntegerField(default=200)   # soft rate limit across workers

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.status})"

class CampaignRecipient(models.Model):
    """Snapshot of who a campaign *intended* to send to (for auditing)."""
    campaign   = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE)
    sent_at    = models.DateTimeField(null=True, blank=True)
    delivered  = models.BooleanField(default=False)
    opened     = models.BooleanField(default=False)
    bounced    = models.BooleanField(default=False)
    error      = models.TextField(blank=True)

    class Meta:
        unique_together = ("campaign", "subscriber")

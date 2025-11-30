# notifications/models.py
from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.utils import timezone

User = get_user_model()


class NotificationQuerySet(models.QuerySet):
    def unread(self):
        return self.filter(is_read=False)

    def read(self):
        return self.filter(is_read=True)

    def mark_all_read(self, recipient=None):
        qs = self.unread()
        if recipient:
            qs = qs.filter(recipient=recipient)
        qs.update(is_read=True)


class NotificationManager(models.Manager):
    def get_queryset(self):
        return NotificationQuerySet(self.model, using=self._db)

    def unread_count_for(self, user):
        return self.get_queryset().unread().filter(recipient=user).count()


class Notification(models.Model):
    # =============================================
    # VERB CHOICES – Grouped for clarity
    # =============================================
    VERB_CHOICES = [
        # ── Vendor / Seller Notifications ──
        ("vendor_new_order", "New Order Received"),
        ("vendor_order_shipped", "You Shipped an Order"),
        ("vendor_order_cancelled", "Order Cancelled"),
        ("vendor_payout", "Payout Processed"),
        ("vendor_low_stock", "Low Stock Alert"),
        ("vendor_new_review", "New Product Review"),
        ("vendor_product_approved", "Product Approved"),
        ("vendor_product_rejected", "Product Rejected"),
        ("vendor_withdrawal_request", "Withdrawal Requested"),
        ("vendor_withdrawal_approved", "Withdrawal Approved"),

        # ── Customer / Buyer Notifications ──
        ("customer_order_placed", "Order Placed"),
        ("customer_order_confirmed", "Order Confirmed"),
        ("customer_order_shipped", "Order Shipped"),
        ("customer_order_delivered", "Order Delivered"),
        ("customer_order_cancelled", "Order Cancelled"),
        ("customer_refund_processed", "Refund Processed"),
        ("customer_tracking_update", "Tracking Updated"),
        ("customer_price_drop", "Price Drop on Watched Item"),
        ("customer_back_in_stock", "Item Back in Stock"),
        ("customer_wishlist_sale", "Wishlist Item on Sale"),
        ("customer_review_reminder", "Please Review Your Purchase"),

        # ── Shared / General ──
        ("message", "New Message"),
        ("announcement", "Announcement"),
        ("support_reply", "Support Ticket Reply"),
        ("verification_update", "Account Verification Update"),
        ("subscription_reminder", "Subscription Expiring Soon"),
    ]

    recipient = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications"
    )
    verb = models.CharField(max_length=40, choices=VERB_CHOICES)

    # Optional: Who triggered it (buyer, vendor, admin, system)
    actor_content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True, related_name="notification_actors"
    )
    actor_object_id = models.PositiveIntegerField(null=True, blank=True)
    actor = GenericForeignKey("actor_content_type", "actor_object_id")

    # Main object (Order, Product, Review, Payout, etc.)
    target_content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True
    )
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_content_type", "target_object_id")

    # Extra flexible data (order number, amount, tracking URL, etc.)
    data = models.JSONField(default=dict, blank=True)

    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    # Custom manager
    objects = NotificationManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "-created_at"]),
            models.Index(fields=["recipient", "is_read"]),
            models.Index(fields=["verb"]),
            models.Index(fields=["target_content_type", "target_object_id"]),
        ]
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"

    def __str__(self):
        return f"{self.recipient} ← {self.get_verb_display()}"

    def mark_as_read(self):
        if not self.is_read:
            self.is_read = True
            self.save(update_fields=["is_read"])
    
    def get_verb_display(self):
        return dict(self.VERB_CHOICES).get(self.verb, self.verb)
    


# models.py
from django.db import models
from django.core.validators import MinLengthValidator
import uuid


class ContactInquiry(models.Model):
    """
    General "Contact Us" form submissions
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # User (optional - allows guest submissions)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_inquiries"
    )

    # Contact info (for guests)
    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)

    # Inquiry details
    class InquiryType(models.TextChoices):
        GENERAL = "general", "General Inquiry"
        ORDER_HELP = "order_help", "Order Assistance"
        RETURNS = "returns", "Returns & Refunds"
        WHOLESALE = "wholesale", "Wholesale / B2B"
        PRESS = "press", "Press & Media"
        PARTNERSHIP = "partnership", "Partnership / Influencer"
        OTHER = "other", "Other"

    inquiry_type = models.CharField(
        max_length=20,
        choices=InquiryType.choices,
        default=InquiryType.GENERAL
    )

    subject = models.CharField(max_length=200)
    message = models.TextField(validators=[MinLengthValidator(20)])

    # Optional order reference
    order = models.ForeignKey(
        "order.Order",  # replace with your actual Order model path
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_inquiries"
    )

    # Admin & status
    class Status(models.TextChoices):
        NEW = "new", "New"
        IN_PROGRESS = "in_progress", "In Progress"
        RESOLVED = "resolved", "Resolved"
        SPAM = "spam", "Spam"

    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.NEW
    )

    admin_note = models.TextField(blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "contact_inquiries"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["inquiry_type"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.subject} - {self.name or self.user}"


class Report(models.Model):
    """
    User reports: abusive reviews, fake products, seller misconduct, copyright, etc.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    reporter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="reports_submitted"
    )

    class ReportReason(models.TextChoices):
        FAKE_PRODUCT = "fake", "Counterfeit / Fake Product"
        INAPPROPRIATE = "inappropriate", "Inappropriate Content"
        SPAM = "spam", "Spam / Scam"
        COPYRIGHT = "copyright", "Copyright / Trademark Violation"
        HARASSMENT = "harassment", "Harassment or Hate Speech"
        SELLER_MISCONDUCT = "seller_misconduct", "Seller Fraud / Misconduct"
        REVIEW_ABUSE = "review_abuse", "Fake or Abusive Review"
        OTHER = "other", "Other"

    reason = models.CharField(max_length=30, choices=ReportReason.choices)
    description = models.TextField(validators=[MinLengthValidator(30)])

    # Polymorphic target (what is being reported)
    content_type = models.ForeignKey(
        ContentType,  # Uses the import above
        on_delete=models.CASCADE,
        limit_choices_to={
            "model__in": ["product", "vendor", "order", "user"]
        },
        help_text="What is being reported (Product, Review, Seller, Order, User, etc.)"
    )
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    # Resolution
    class ResolutionStatus(models.TextChoices):
        PENDING = "pending", "Under Review"
        VALID = "valid", "Action Taken"
        REJECTED = "rejected", "Rejected"
        ESCALATED = "escalated", "Escalated"

    status = models.CharField(
        max_length=15,
        choices=ResolutionStatus.choices,
        default=ResolutionStatus.PENDING
    )

    moderator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports_handled"
    )
    moderator_note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reports"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["reason"]),
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reporter", "content_type", "object_id", "reason"],
                name="unique_report_per_user_target_reason"
            )
        ]

    def __str__(self):
        return f"Report by {self.reporter} - {self.reason} - {self.target}"
    

class SupportTicket(models.Model):
    ticket_id = models.CharField(max_length=20, unique=True, editable=False)
    inquiry = models.OneToOneField(ContactInquiry, on_delete=models.CASCADE)
    assigned_to = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    priority = models.CharField(max_length=10, choices=[("low","Low"), ("medium","Medium"), ("high","High"), ("urgent","Urgent")], default="medium")
    
    def save(self, *args, **kwargs):
        if not self.ticket_id:
            self.ticket_id = f"TKT-{timezone.now().strftime('%Y%m%d')}-{SupportTicket.objects.count()+1:04d}"
        super().save(*args, **kwargs)

# models.py — add this new model
class TicketReply(models.Model):
    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name='replies')
    replied_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    message = models.TextField()
    is_internal = models.BooleanField(default=False, help_text="Only visible to staff")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Reply to {self.ticket.ticket_id} by {self.replied_by or 'Staff'}"

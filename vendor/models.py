from django.db import models
from userauths.models import User, Profile
from django.core.mail import EmailMessage
from django.utils import timezone
from datetime import time, date, datetime
from django.urls import reverse
from django.db.models.signals import post_save
from django.template.loader import render_to_string
from django.utils.text import slugify
from datetime import timedelta
from django_countries.fields import CountryField
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
import requests
import json
import exifread
import logging
from address.models import Country
from django.conf import settings
from avatar_generator import Avatar
import requests
from django.conf import settings
import base64
from django.core.validators import MinLengthValidator, RegexValidator
from PIL import Image, ImageEnhance
import easyocr
import numpy as np
from .tasks import send_vendor_approval_email, send_vendor_sms
_reader = None

logger = logging.getLogger(__name__)

class VendorManager(models.Manager):
    def verified(self):
        """Return verified, approved, non-suspended vendors with active subscriptions."""
        today = timezone.now().date()
        return self.filter(
            status='VERIFIED',
            is_approved=True,
            is_suspended=False,
            is_subscribed=True,
            subscription_end_date__gte=today
        )

    def needs_review(self):
        """Return vendors pending admin review."""
        return self.filter(status='PENDING')

    def recently_approved(self, days=30):
        """Return vendors approved within the last N days."""
        since = timezone.now().date() - timedelta(days=days)
        return self.filter(
            status='VERIFIED',
            is_approved=True,
            modified_at__gte=since
        )

    def rejected(self):
        """Return rejected vendors for admin review."""
        return self.filter(status='REJECTED')

    def suspended(self):
        """Return suspended vendors."""
        return self.filter(status='SUSPENDED', is_suspended=True)

    # def get_queryset(self):
    #     """Default queryset for non-admin users: only verified, approved, non-suspended vendors."""
    #     today = timezone.now().date()
    #     return super().get_queryset().filter(
    #         # status='VERIFIED',
    #         is_approved=True,
    #         is_suspended=False,
    #         is_subscribed=True,
    #         # subscription_end_date__gte=today
    #     )

class Vendor(models.Model):
    # Constants for Choices
    VENDOR_TYPE_CHOICES = [
        ('student', 'Student'),
        ('non_student', 'Non-Student'),
    ]

    BUSINESS_TYPE_CHOICES = [
        ('sole_proprietor', 'Sole Proprietor'),
        ('partnership', 'Partnership'),
        ('corporation', 'Corporation'),
        ('llc', 'Limited Liability Company (LLC)'),
        ('non_profit', 'Non-Profit'),
        ('other', 'Other'),
    ]

    VERIFICATION_STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('VERIFIED', 'Verified'),
        ('REJECTED', 'Rejected'),
        ('SUSPENDED', 'Suspended'),
    ]
    objects = VendorManager()

    # Basic Vendor Information
    name = models.CharField(
        max_length=128,
        unique=True,
        validators=[MinLengthValidator(2)],
        help_text="The legal or trading name of the vendor."
    )
    slug = models.SlugField(max_length=256, unique=True, editable=False)
    user = models.OneToOneField(
        User,
        related_name='vendor_user',
        on_delete=models.CASCADE,
        unique=True,
        help_text="Linked user account for this vendor."
    )
    email = models.EmailField(
        max_length=128,
        unique=True,
        blank=True,
        null=True,
        help_text="Primary contact email for the vendor."
    )
    country = CountryField(
        blank_label="Select country",
        default='GH',
        help_text="Country where the vendor operates."
    )
    shipping_from_country = models.ForeignKey(Country, on_delete=models.SET_NULL, null=True, blank=True)
    contact = models.CharField(
        max_length=20,
        validators=[RegexValidator(r'^\+?1?\d{9,15}$', 'Enter a valid phone number with country code (e.g., +233XXXXXXXXX)')],
        default="+233",
        help_text="Contact phone number with country code."
    )
    followers = models.ManyToManyField(User, related_name='vendor_following', blank=True)

    # Business Documents
    license = models.FileField(
        upload_to='vendor/license/',
        blank=True,
        null=True,
        help_text="Business license for non-student vendors."
    )
    student_id = models.FileField(
        upload_to='vendor/studentid/',
        blank=True,
        null=True,
        help_text="Student ID for student vendors."
    )
    proof_of_address = models.FileField(
        upload_to='vendor_addresses/',
        blank=True,
        null=True,
        help_text="Proof of address (e.g., utility bill) dated within 180 days."
    )
    government_issued_id = models.FileField(
        upload_to='vendor/id_photos/',
        blank=True,
        null=True,
        help_text="Photo of government-issued ID (e.g., ID card, passport, driver's license)."
    )

    # Verification and Status
    status = models.CharField(
        max_length=20,
        choices=VERIFICATION_STATUS_CHOICES,
        default='PENDING',
        help_text="Current status of identity."
    )
    # Business and Subscription Details
    is_suspended = models.BooleanField(default=False, help_text="Indicates if the vendor is suspended.")
    is_featured = models.BooleanField(default=False, help_text="Indicates if the vendor is featured.")
    is_approved = models.BooleanField(default=False, help_text="Indicates if the vendor is approved.")
    is_subscribed = models.BooleanField(default=False, help_text="Indicates if the vendor has an active subscription.")
    subscription_start_date = models.DateField(blank=True, null=True, help_text="Date when subscription starts.")
    subscription_end_date = models.DateField(blank=True, null=True, help_text="Date when subscription ends.")
    is_manufacturer = models.BooleanField(default=False, help_text="Indicates if the vendor is a manufacturer.")

    vendor_type = models.CharField(
        max_length=20,
        choices=VENDOR_TYPE_CHOICES,
        default='student',
        help_text="Type of vendor (student or non-student)."
    )
    business_type = models.CharField(
        max_length=50,
        choices=BUSINESS_TYPE_CHOICES,
        default='sole_proprietor',
        help_text="Legal structure of the business."
    )

    # Analytics and Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    views = models.PositiveIntegerField(default=0, help_text="Number of views on vendor profile.")

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('core:vendor_detail', args=[self.slug])

    def has_active_subscription(self):
        """Check if the vendor has an active subscription."""
        return self.is_subscribed and self.subscription_end_date and self.subscription_end_date >= timezone.now().date()

    def subscription_due_soon(self):
        """Check if the subscription is due within the next 7 days."""
        if self.is_subscribed and self.subscription_end_date:
            return self.subscription_end_date <= timezone.now().date() + timedelta(days=7)
        return False

    def is_open(self):
        """Check if the vendor is open based on current day and time."""
        from .models import OpeningHour  # Avoid circular import
        today = timezone.now().date().isoweekday()
        current_time = timezone.now().time()
        today_operating_hours = OpeningHour.objects.filter(vendor=self, day=today, is_closed=False)
        return any(hours.from_hour and hours.to_hour and hours.from_hour <= current_time <= hours.to_hour
                   for hours in today_operating_hours)

    def _encode_image(self, image_path):
        """Encode image file to base64 for API upload."""
        try:
            with open(image_path, 'rb') as img_file:
                return base64.b64encode(img_file.read()).decode('utf-8')
        except Exception as e:
            logger.error(f"Image encoding error for {image_path}: {e}")
            raise ValidationError("Failed to process image file.")

    def save(self, *args, **kwargs):
        """Override save to generate slug and handle approval logic."""
        if not self.slug:
            base_slug = slugify(self.name)
            self.slug = base_slug
            counter = 1
            while Vendor.objects.filter(slug=self.slug).exists():
                self.slug = f"{base_slug}-{counter}"
                counter += 1
        
        if self.pk:
            previous_state = Vendor.objects.get(pk=self.pk)
            if previous_state.is_approved != self.is_approved:
                send_vendor_approval_email.delay(self.id, self.is_approved)
                send_vendor_sms.delay(self.id, self.is_approved)
                if self.is_approved:
                    self.user.role = 'vendor'  # Change user role to 'vendor'
                    self.user.save()
                else:
                    self.user.role = 'customer'
                    self.user.save()

        super().save(*args, **kwargs)

    def clean(self):
        """Validate vendor data before saving."""
        if self.vendor_type == 'student' and not self.student_id:
            raise ValidationError("Students must upload a valid student ID.")
        if self.vendor_type == 'non_student' and not self.government_issued_id:
            raise ValidationError("Non-students must upload a valid business license.")
        if self.status != 'PENDING' and not (self.proof_of_address):
            raise ValidationError("Both ID photo and selfie with ID are required for verification.")
        # if self.proof_of_address and not self._is_proof_within_180_days():
        #     raise ValidationError("Proof of address must be dated within the last 180 days.")


    def _is_proof_within_180_days(self):
        """Check if proof of address is within 180 days based on image content."""
        global _reader
        if _reader is None:
            try:
                _reader = easyocr.Reader(['en'], gpu=False)  # Initialize once
                logger.debug("EasyOCR reader initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize EasyOCR reader: {e}")
                return False

        if not self.proof_of_address:
            return True  # No proof provided, skip validation (handled by clean())

        current_date = datetime.now().date()
        max_allowed_date = current_date - timedelta(days=180)

        try:
            # Restrict to image files only
            if not self.proof_of_address.name.lower().endswith(('.jpg', '.jpeg', '.png')):
                logger.error(f"Unsupported file type: {self.proof_of_address.name}")
                return False

            # Copy file content into memory
            file_content = BytesIO(self.proof_of_address.read())
            self.proof_of_address.seek(0)

            # Use EasyOCR to extract text
            try:
                image = Image.open(file_content)
                # Preprocess image
                if image.mode != 'RGB':
                    image = image.convert('RGB')
                # Convert to grayscale
                image = image.convert('L')  # Grayscale
                # Enhance contrast
                image = ImageEnhance.Contrast(image).enhance(3.0)  # Increased contrast
                # Resize to improve OCR performance
                image = image.resize((1000, 1000), Image.LANCZOS)

                # Convert PIL image to NumPy array for EasyOCR
                image_np = np.array(image)

                # Extract text with rotation handling
                results = _reader.readtext(image_np, detail=0, rotation_info=[90, 180, 270])
                text = ' '.join(results)  # Combine all text fragments
                logger.debug(f"OCR extracted text for {self.proof_of_address.name}: {text[:1000]}")  # Log first 1000 chars

                # Search for common date formats
                date_match = re.search(
                    r"\d{4}[-/]\d{2}[-/]\d{2}"  # YYYY-MM-DD or YYYY/MM/DD
                    r"|\d{2}[-/]\d{2}[-/]\d{4}"  # DD-MM-YYYY or DD/MM/YYYY
                    r"|\d{2}\s+[A-Za-z]{3}\s+\d{4}"  # DD Mon YYYY
                    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}"  # Month DD, YYYY
                    r"|\d{2}\.\d{2}\.\d{4}"  # DD.MM.YYYY
                    r"|\d{2}/\d{2}/\d{2}"  # MM/DD/YY
                    r"|\d{8}",  # YYYYMMDD
                    text
                )
                if date_match:
                    date_str = date_match.group(0)
                    try:
                        if re.match(r"\d{2}\s+[A-Za-z]{3}\s+\d{4}", date_str):  # DD Mon YYYY
                            date_obj = datetime.strptime(date_str, "%d %b %Y").date()
                        elif re.match(r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}", date_str):  # Month DD, YYYY
                            date_str = date_str.replace(',', '')
                            date_obj = datetime.strptime(date_str, "%B %d %Y").date()
                        elif re.match(r"\d{2}\.\d{2}\.\d{4}", date_str):  # DD.MM.YYYY
                            date_obj = datetime.strptime(date_str, "%d.%m.%Y").date()
                        elif re.match(r"\d{2}/\d{2}/\d{2}", date_str):  # MM/DD/YY
                            date_obj = datetime.strptime(date_str, "%m/%d/%y").date()
                        elif re.match(r"\d{8}", date_str):  # YYYYMMDD
                            date_obj = datetime.strptime(date_str, "%Y%m%d").date()
                        elif date_str[2] in ['-', '/']:  # DD-MM-YYYY or DD/MM/YYYY
                            date_obj = datetime.strptime(date_str, "%d-%m-%Y").date()
                        else:  # YYYY-MM-DD or YYYY/MM/DD
                            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                        logger.info(f"Extracted valid date: {date_obj} from {self.proof_of_address.name}")
                        return date_obj >= max_allowed_date
                    except ValueError:
                        logger.warning(f"Invalid date format found in OCR: {date_str}")
                        return False
                else:
                    logger.warning(f"No valid date found in OCR text for {self.proof_of_address.name}")
                    return False

            except Exception as e:
                logger.error(f"OCR processing failed for {self.proof_of_address.name}: {e}")
                return False

        except Exception as e:
            logger.error(f"Error processing proof of address file {self.proof_of_address.name}: {e}")
            return False

    class Meta:
        verbose_name = "Vendor"
        verbose_name_plural = "Vendors"
        indexes = [
            models.Index(fields=['name', 'status']),
        ]


# vendor payment method model
from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django_countries.fields import CountryField
from userauths.models import User
import re

class VendorPaymentMethod(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ('momo', 'Mobile Money'),
        ('bank', 'Bank Transfer'),
        ('paypal', 'PayPal'),
    ]
    MOMO_PROVIDER_CHOICES = [
        ('MTN', 'MTN'),
        ('VODAFONE', 'Vodafone'),
        ('AIRTELTIGO', 'AirtelTigo'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('verified', 'Verified'),
        ('rejected', 'Rejected'),
    ]

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='payment_methods')
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    momo_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        validators=[
            RegexValidator(
                regex=r'^\+?\d{10,15}$',
                message="Mobile Money number must be a valid phone number (10-15 digits, optional + prefix).",
                code='invalid_momo_number'
            )
        ]
    )
    momo_provider = models.CharField(max_length=20, choices=MOMO_PROVIDER_CHOICES, blank=True, null=True)
    bank_name = models.CharField(max_length=100, blank=True, null=True)
    bank_account_name = models.CharField(max_length=100, blank=True, null=True)
    bank_account_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        validators=[
            RegexValidator(
                regex=r'^\d{8,50}$',
                message="Bank account number must be 8-50 digits.",
                code='invalid_bank_account_number'
            )
        ]
    )
    country = models.CharField(max_length=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='payment_methods_updated'
    )

    def __str__(self):
        return f"{self.vendor} - {self.get_payment_method_display()}"

    def clean(self):
        """
        Validate that required fields are provided based on the selected payment method.
        """
        errors = {}
        if self.payment_method == 'momo':
            if not self.momo_number:
                errors['momo_number'] = "Mobile Money number is required."
            if not self.momo_provider:
                errors['momo_provider'] = "Mobile Money provider is required."
        elif self.payment_method == 'bank':
            if not self.bank_name:
                errors['bank_name'] = "Bank name is required."
            if not self.bank_account_name:
                errors['bank_account_name'] = "Bank account name is required."
            if not self.bank_account_number:
                errors['bank_account_number'] = "Bank account number is required."
        elif self.payment_method == 'paypal':
            if not self.momo_number:
                errors['momo_number'] = "PayPal email or phone is required."
            elif not re.match(r'^\S+@\S+\.\S+$|^\+?\d{10,15}$', self.momo_number):
                errors['momo_number'] = "PayPal email or phone must be a valid email or 10-15 digit phone number."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import time

class OpeningHour(models.Model):
    DAYS = [
        (1, 'Monday'),
        (2, 'Tuesday'),
        (3, 'Wednesday'),
        (4, 'Thursday'),
        (5, 'Friday'),
        (6, 'Saturday'),
        (7, 'Sunday'),
    ]

    vendor = models.ForeignKey('Vendor', on_delete=models.CASCADE, related_name='opening_hours')
    day = models.IntegerField(choices=DAYS)
    from_hour = models.TimeField(null=True, blank=True)
    to_hour = models.TimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)

    class Meta:
        ordering = ('day', 'from_hour')
        unique_together = ('vendor', 'day', 'from_hour', 'to_hour')

    def clean(self):
        if not self.is_closed:
            if not self.from_hour or not self.to_hour:
                raise ValidationError("Both 'From Hour' and 'To Hour' are required if the day is not closed.")
            if self.from_hour >= self.to_hour:
                raise ValidationError("'From Hour' must be earlier than 'To Hour'.")
        else:
            self.from_hour = None
            self.to_hour = None

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        if self.is_closed:
            return f"{self.get_day_display()} (Closed)"
        return f"{self.get_day_display()}: {self.from_hour.strftime('%I:%M %p')} - {self.to_hour.strftime('%I:%M %p')}"
    
class Message(models.Model):
    body = models.TextField()
    sent_by = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='messages', blank=True, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ('created_at',)

def user_directory_path(instance, filename):
    return 'user_{0}/{1}'.format(instance.vendor.id, filename)


class About(models.Model):
    vendor = models.OneToOneField(Vendor, on_delete=models.CASCADE, related_name='about')
    profile_image = models.ImageField(upload_to=user_directory_path, blank=True)
    cover_image = models.ImageField(upload_to='vendor/cover_image', default='vendor/cover.png', blank=True)
    address = models.CharField(max_length=200, default="123 Main street, Suame")
    about = models.TextField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True, default=5.5600)
    longitude = models.FloatField(null=True, blank=True, default=-0.2050)
    shipping_on_time = models.CharField(max_length=200, default="100")
    chat_resp_time = models.CharField(max_length=200, default="100")
    authentic_rating = models.CharField(max_length=200, default="100")
    day_return = models.CharField(max_length=200, default="100")
    waranty_period = models.CharField(max_length=200, default="100")
    facebook_url = models.CharField(max_length=50, blank=True)
    instagram_url = models.CharField(max_length=50, blank=True)
    twitter_url = models.CharField(max_length=50, blank=True)
    linkedin_url = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return self.vendor.user.email
    
    def save(self, *args, **kwargs):
        super(About, self).save(*args, **kwargs)
        if not self.profile_image:
            self.generate_initials_profile_picture()
    
    def generate_initials_profile_picture(self):
        # Get initials or fallback
        name = self.vendor.name or "Sample Seller"

        # avatar-generator returns PNG bytes directly
        avatar_bytes = Avatar.generate(200, name)

        # Save image to ImageField
        self.profile_image.save(
            f"{self.vendor.email}_profile.png",
            ContentFile(avatar_bytes),
            save=True
        )
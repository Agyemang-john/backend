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
import logging
from django.conf import settings
from avatar_generator import Avatar

logger = logging.getLogger(__name__)




DAYS = [
    (1, 'Monday'),
    (2, 'Tuesday'),
    (3, 'Wednesday'),
    (4, 'Thursday'),
    (5, 'Friday'),
    (6, 'Saturday'),
    (7, 'Sunday'),
]


TIME = [(time(h, m).strftime('%I:%M %p'), time(h, m).strftime('%I:%M %p'))
        for h in range(24) for m in (0, 30)]


class Vendor(models.Model):

    VENDOR_TYPE_CHOICES = [
        ('student', 'Student'),
        ('non_student', 'Non-Student'),
    ]

    name = models.CharField(max_length=128, unique=True)
    slug = models.SlugField(max_length=256, unique=True)
    user = models.OneToOneField(User, related_name='vendor_user', on_delete=models.CASCADE, unique=True)
    email = models.EmailField(max_length=128, unique=True, blank=True, null=True)
    country = CountryField(blank_label="select country", default='GH')
    followers = models.ManyToManyField(User, related_name='vendor_following', blank=True)
    license = models.FileField(upload_to='vendor/license', blank=True, null=True)
    student_id = models.FileField(upload_to='vendor/studentid', blank=True, null=True)
    contact = models.CharField(max_length=200, default="+233")
    is_featured = models.BooleanField(default=False)
    is_approved = models.BooleanField(default=False)
    is_subscribed = models.BooleanField(default=False)  # Add subscription status field
    subscription_end_date = models.DateField(blank=True, null=True)

    vendor_type = models.CharField(
        max_length=20,
        choices=VENDOR_TYPE_CHOICES,
        default='student',
    )

    business_type_choices = [
        ('sole_proprietor', 'Sole Proprietor'),
        ('partnership', 'Partnership'),
        ('corporation', 'Corporation'),
        ('llc', 'Limited Liability Company (LLC)'),
        ('non_profit', 'Non-Profit'),
        ('other', 'Other'),
    ]
    business_type = models.CharField(
        max_length=50, 
        choices=business_type_choices, 
        default='sole_proprietor'
    )


    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name
    
    def get_absolute_url(self):
        return reverse('core:vendor_detail', args=[self.slug])
    
    def has_active_subscription(self):
        """Check if the vendor has an active subscription."""
        if self.is_subscribed and self.subscription_end_date:
            return self.subscription_end_date >= timezone.now().date()
        return False

    def subscription_due_soon(self):
        """Check if the subscription is due within the next 7 days."""
        if self.is_subscribed and self.subscription_end_date:
            return self.subscription_end_date <= timezone.now().date() + timedelta(days=7)
        return False

    def is_open(self):
        today = date.today().isoweekday()
        today_operating_hours = OpeningHour.objects.filter(vendor=self, day=today, is_closed=False)
        current_time = timezone.now().strftime('%H:%M:%S')

        for hours in today_operating_hours:
            start_time = datetime.strptime(hours.from_hour, '%I:%M %p').time().strftime('%H:%M:%S')
            end_time = datetime.strptime(hours.to_hour, '%I:%M %p').time().strftime('%H:%M:%S')
            if start_time < current_time < end_time:
                return True
        return False

    def save(self, *args, **kwargs):
        # Generate slug from name if not provided
        if not self.slug:
            self.slug = slugify(self.name)
            # Ensure the slug is unique
            original_slug = self.slug
            counter = 1
            while Vendor.objects.filter(slug=self.slug).exists():
                self.slug = f"{original_slug}-{counter}"
                counter += 1

        # Handle approval changes
        if self.pk:
            previous_state = Vendor.objects.get(pk=self.pk)
            if previous_state.is_approved != self.is_approved:
                # self.send_approval_email()
                # self.send_sms()
                if self.is_approved:
                    self.user.role = 'vendor'  # Change user role to 'vendor'
                    self.user.save()
                else:
                    self.user.role = 'customer'
                    self.user.save()

        super().save(*args, **kwargs)
    
    def clean(self):
        # Ensure either a license or student ID is provided based on vendor type
        if self.vendor_type == 'student' and not self.student_id:
            raise ValidationError("Students must upload a valid student ID.")
        if self.vendor_type == 'non_student' and not self.license:
            raise ValidationError("Non-students must upload a valid business license.")
    
    
    def send_sms(self):
        if self.is_approved:
            mail_subject = "Congratulations! Your shop has been approved"
        else:
            mail_subject = "We're sorry! Your shop is not eligible"
        api_key = "SnJLekVvY2l6UVRacXRoZmNBdnY"
        sender_id = 'Negromart'

        base_url = f"https://sms.arkesel.com/sms/api?action=send-sms&api_key={api_key}&from={sender_id}"
        sms_url = base_url + f"&to={self.contact}&sms={mail_subject}"

        response = requests.get(sms_url)
        response_json = response.json()
        return response_json

    def send_approval_email(self):
        """
        Send an email to the vendor informing them of their shop approval status.
        The email is either congratulatory if approved, or informative if not approved.
        """
        # Determine the subject and template based on the approval status
        if self.is_approved:
            mail_subject = "Congratulations! Your shop has been approved"
            mail_template = 'email/store-approval-email.html'
        else:
            mail_subject = "We're sorry! Your shop is not eligible"
            mail_template = 'email/store-denied-email.html'

        # Prepare the context for rendering the template
        context = {
            'user': self.user,
            'is_approved': self.is_approved,
            'to_email': self.email,
        }

        # Render the HTML content for the email
        try:
            email_message = render_to_string(mail_template, context)
        except Exception as e:
            logger.error(f"Error rendering email template {mail_template}: {e}")
            return False  # Return False or handle this gracefully

        # Set the sender email address. You can use settings for this if needed.
        from_email = settings.DEFAULT_FROM_EMAIL if settings.DEFAULT_FROM_EMAIL else 'ecommerceplatform35@gmail.com'

        # Create the email message
        email = EmailMessage(
            subject=mail_subject,
            body=email_message,
            from_email=from_email,
            to=[self.email],
        )

        # Ensure the email is sent as HTML
        email.content_subtype = 'html'

        # Try sending the email and handle any exceptions
        try:
            email.send(fail_silently=False)
            logger.info(f"Approval email sent to {self.email}")
            return True  # Return True when email is sent successfully
        except Exception as e:
            logger.error(f"Error sending approval email to {self.email}: {e}")
            return False  # Return False if there was an error in sending the email



class VendorPaymentMethod(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ('momo', 'Mobile Money'),
        ('bank', 'Bank Transfer'),
    ]

    vendor = models.OneToOneField(
        'Vendor',
        related_name='payment_method',
        on_delete=models.CASCADE
    )
    payment_method = models.CharField(
        max_length=10,
        choices=PAYMENT_METHOD_CHOICES,
        default='momo'
    )
    
    # Mobile Money Details
    momo_number = models.CharField(max_length=15, blank=True, null=True)  # e.g., 233XXX
    momo_provider = models.CharField(max_length=50, blank=True, null=True)  # e.g., MTN, AirtelTigo, Vodafone
    
    # Bank Details
    bank_name = models.CharField(max_length=128, blank=True, null=True)
    bank_account_name = models.CharField(max_length=128, blank=True, null=True)
    bank_account_number = models.CharField(max_length=64, blank=True, null=True)
    bank_routing_number = models.CharField(max_length=64, blank=True, null=True)  # Optional
    
    # Other Fields
    country = CountryField(blank_label="Select country", default="GH")  # For regional validation
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.vendor.name} - {self.get_payment_method_display()}"

    def clean(self):
        """
        Validate that required fields are provided based on the selected payment method.
        """
        from django.core.exceptions import ValidationError
        if self.payment_method == 'momo' and not (self.momo_number and self.momo_provider):
            raise ValidationError("Mobile Money details must be provided.")
        elif self.payment_method == 'bank' and not (self.bank_name and self.bank_account_name and self.bank_account_number):
            raise ValidationError("Bank details must be provided.")

class OpeningHour(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    day = models.IntegerField(choices=DAYS)
    from_hour = models.CharField(choices=TIME, max_length=16, blank=True)
    to_hour = models.CharField(choices=TIME, max_length=16, blank=True)
    is_closed = models.BooleanField(default=False)

    class Meta:
        ordering = ('day', '-from_hour')
        unique_together = ('vendor', 'day', 'from_hour', 'to_hour')

    def __str__(self):
        return self.get_day_display()
    
class Message(models.Model):
    body = models.TextField()
    sent_by = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, blank=True, null=True, on_delete=models.SET_NULL)

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
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
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
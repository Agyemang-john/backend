from enum import unique
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db.models.signals import post_save
from django.utils import timezone
from avatar_generator import Avatar
from django.utils.translation import gettext_lazy as _
from django.core.files.base import ContentFile
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from rest_framework_simplejwt.tokens import RefreshToken
import hashlib
import re
# from address.models import Country, Region, Town

ROLE_CHOICES = (
    ('vendor', 'Vendor'),
    ('customer', 'Customer'),
    ('technical', 'Technical'),
    ('support', 'Support'),
    ('manager', 'Manager'),
    ('admin', 'Admin'),
)

class UserManager(BaseUserManager):
    def create_user(self, first_name, last_name, email, phone, password=None):
        if not email:
            raise ValueError('Please provide an email address')

        if not phone:
            raise ValueError('Please provide a phone number')

        user = self.model(
            email=self.normalize_email(email),
            phone=phone,
            first_name=first_name,
            last_name=last_name,
        )
        user.role = 'customer'
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, first_name, last_name, email, phone, password=None):
        user = self.create_user(
            email=self.normalize_email(email),
            phone=phone,
            first_name=first_name,
            last_name=last_name,
            password=password,
        )
        user.role = 'admin'  # Assign a default role for superuser, adjust as needed
        user.is_superuser = True
        user.is_active = True
        user.is_admin = True
        user.is_staff = True
        user.save(using=self._db)
        return user

AUTH_PROVIDER = { 
    'email': 'email',
    'google': 'google',
}

class User(AbstractBaseUser, PermissionsMixin):
    first_name = models.CharField(max_length=128)
    last_name = models.CharField(max_length=128)
    email = models.EmailField(max_length=128, unique=True)
    phone = models.CharField(max_length=32, unique=True)
    role = models.CharField(max_length=32, choices=ROLE_CHOICES, blank=True, null=True)
    date_joined = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(auto_now=True)
    created_date = models.DateTimeField(auto_now_add=True)
    modified_date = models.DateTimeField(auto_now=True)
    is_suspended = models.BooleanField(default=False)
    is_active = models.BooleanField(default=False)
    is_admin = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    failed_login_attempts = models.PositiveIntegerField(default=0)
    lockout_until = models.DateTimeField(null=True, blank=True)


    auth_provider = models.CharField(max_length=50, default=AUTH_PROVIDER.get('email'))

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name', 'phone']

    objects = UserManager()

    def __str__(self):
        return self.email

    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return True
    
    def tokens(self):
        refresh = RefreshToken.for_user(self)  # Pass the user instance
        return {
            'refresh': str(refresh),
            'access': str(refresh.access_token),
        }

def user_directory_path(instance, filename):
    return 'users/user_{0}/{1}'.format(instance.user.id, filename)

class Profile(models.Model):
    user = models.OneToOneField(User, related_name='profile', on_delete=models.CASCADE)
    profile_image = models.ImageField(upload_to=user_directory_path, blank=True, null=True)
    mobile = models.CharField(max_length=15, blank=True, null=True)
    country = models.CharField(max_length=100, blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    gender = models.CharField(max_length=10, choices=[('Male', 'Male'), ('Female', 'Female'), ('Other','Other')], blank=True, null=True)
    address = models.CharField(max_length=900, blank=True, null=True)
    newsletter_subscription = models.BooleanField(default=False)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    modified_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.email

    def save(self, *args, **kwargs):
        super(Profile, self).save(*args, **kwargs)
        if not self.profile_image:
            self.generate_initials_profile_picture()

    def generate_initials_profile_picture(self):
        if self.profile_image:
            return

        # Decide what text to use for initials generation
        if self.user.first_name and self.user.last_name:
            text = f"{self.user.first_name} {self.user.last_name}"
        elif self.user.first_name:
            text = self.user.first_name
        elif self.user.last_name:
            text = self.user.last_name
        else:
            text = self.user.email or "User"

        # Generate avatar as bytes
        avatar_bytes = Avatar.generate(200, text)  # returns bytes

        # Wrap bytes into PIL Image
        avatar_image = Image.open(BytesIO(avatar_bytes))

        # Save to buffer
        buffer = BytesIO()
        avatar_image.save(buffer, format="PNG")
        buffer.seek(0)

        # Build safe filename
        base_name = self.user.email.split("@")[0] if self.user.email else "user"
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", base_name)
        filename = f"{safe_name}_avatar.png"

        # Save to ImageField
        self.profile_image.save(filename, ContentFile(buffer.read()), save=True)


class ContactUs(models.Model):
    full_name = models.CharField(max_length=200)
    email = models.CharField(max_length=200)
    phone = models.CharField(max_length=200) # +234 (456) - 789
    subject = models.CharField(max_length=200) # +234 (456) - 789
    message = models.TextField()

    class Meta:
        verbose_name = "Contact Us"
        verbose_name_plural = "Contact Us"

    def __str__(self):
        return self.full_name
    
class SubscribedUsers(models.Model):
    email = models.EmailField(unique=True, max_length=100)
    created_date = models.DateTimeField('Date created', default=timezone.now)

    def __str__(self):
        return self.email
    
class MailMessage(models.Model):
    title = models.CharField(max_length=200, null=True)
    message = models.TextField()


    def __str__(self):
        return self.title






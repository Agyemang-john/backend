"""
userauths/models.py
Core user and profile models for the platform:
- User: custom user model with email as USERNAME_FIELD, supports email+phone login,
  platform roles (customer, support, manager, admin, ...), contact verification
  timestamps, and login lockout after failed attempts.
  Selling is NOT a role: a user "is a vendor" while they hold an active
  membership in an approved store (see vendor/access.py). One account can
  therefore shop and sell at the same time, the way Amazon/eBay accounts work.
- Profile: one-to-one extension of User with address, avatar generation, and preferences.
- ContactUs: stores contact-form submissions.
- SubscribedUsers / MailMessage: legacy newsletter models.
"""

import hashlib
import hmac
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone
from avatar_generator import Avatar
from django.utils.translation import gettext_lazy as _
from django.core.files.base import ContentFile
from PIL import Image
from io import BytesIO
from rest_framework_simplejwt.tokens import RefreshToken
import re
import uuid

# Platform-level roles. 'vendor' stays only so historical rows and forms remain
# valid; store access is decided by VendorMember, never by this field.
ROLE_CHOICES = (
    ('vendor', 'Vendor (legacy, not used for access)'),
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
        if not password:
            raise ValueError("Superuser must have a password")
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
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    failed_login_attempts = models.PositiveIntegerField(default=0)
    lockout_until = models.DateTimeField(null=True, blank=True)
    token_version = models.PositiveIntegerField(default=0)

    # Contact verification. Each is stamped the moment the user proves control
    # of that email/phone (one-time code, or the email activation link).
    # Opening a store requires both, mirroring Amazon's seller onboarding.
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)

    auth_provider = models.CharField(max_length=50, default=AUTH_PROVIDER.get('email'))

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name', 'phone']

    objects = UserManager()

    def __str__(self):
        return self.email
    
    class Meta:
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['phone']),
        ]

    
    # ── Verification ─────────────────────────────────────────────────────────

    @classmethod
    def from_db(cls, db, field_names, values):
        # Remember the values as loaded so save() can tell what changed.
        instance = super().from_db(db, field_names, values)
        loaded = dict(zip(field_names, values))
        # Only track the phone when its stamp was loaded too (not deferred).
        instance._loaded_phone = loaded.get('phone') if 'phone_verified_at' in loaded else None
        instance._loaded_phone_verified_at = loaded.get('phone_verified_at')
        instance._loaded_is_active = loaded.get('is_active')
        return instance

    def save(self, *args, **kwargs):
        """
        Keep the verification stamps honest whatever code path edits the user
        (customer profile form, admin, djoser, shell):

        - A new phone number is unverified, unless the caller stamped
          phone_verified_at in the same save (verification.confirm_phone).
        - An account becoming active has proven its email (activation link,
          seller-signup code), or an admin vouched for it, so stamp it.
        """
        changed = set()
        loaded_phone = getattr(self, '_loaded_phone', None)
        if self.pk and loaded_phone is not None and self.phone != loaded_phone:
            # Stamp untouched since load → the number changed without proof.
            if self.phone_verified_at == getattr(self, '_loaded_phone_verified_at', None):
                self.phone_verified_at = None
                changed.add('phone_verified_at')
        if self.is_active and getattr(self, '_loaded_is_active', None) is False and self.email_verified_at is None:
            self.email_verified_at = timezone.now()
            changed.add('email_verified_at')

        update_fields = kwargs.get('update_fields')
        if update_fields is not None and changed:
            kwargs['update_fields'] = set(update_fields) | changed
        super().save(*args, **kwargs)
        self._loaded_phone = self.phone
        self._loaded_phone_verified_at = self.phone_verified_at
        self._loaded_is_active = self.is_active

    @property
    def is_email_verified(self):
        # Google sign-ins arrive with an address Google has already verified.
        return self.email_verified_at is not None or (
            self.auth_provider == AUTH_PROVIDER['google'] and self.is_active
        )

    @property
    def is_phone_verified(self):
        return self.phone_verified_at is not None

    # ── Store access ─────────────────────────────────────────────────────────
    # Thin wrappers over vendor/access.py, imported lazily because the vendor
    # app depends on this module, not the other way round. The lookup is
    # cached on the instance, so repeated checks in one request cost one query.

    @property
    def vendor_membership(self):
        """This user's active VendorMember row, or None."""
        from vendor.access import get_membership
        return get_membership(self)

    @property
    def current_vendor(self):
        """
        The store this user works for (as owner, admin or staff), whatever its
        approval status. Raises NoVendorMembership, a subclass of both
        Vendor.DoesNotExist and AttributeError, so `except Vendor.DoesNotExist`
        and `getattr(user, 'current_vendor', None)` both behave as expected.
        """
        from vendor.access import get_current_vendor
        return get_current_vendor(self)

    @property
    def is_vendor(self):
        """True when the user may use the seller dashboard right now."""
        from vendor.access import is_vendor
        return is_vendor(self)

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


class OTPRecord(models.Model):
    """
    Database-backed OTP storage.

    Replaces Redis cache for OTP persistence so that transient Redis failures
    (connection pool exhaustion, auth errors, eviction under memory pressure)
    never silently break the login flow.  The database is ACID-compliant and
    will never return None for a record that was just inserted.

    `purpose` keeps independent flows apart: issuing a phone-verification code
    must not wipe out a pending seller-login code, and a login code must never
    be accepted as proof of owning a phone number. See userauths/verification.py.
    """

    PURPOSE_VENDOR_LOGIN = 'vendor_login'
    PURPOSE_EMAIL_VERIFY = 'email_verify'
    PURPOSE_PHONE_VERIFY = 'phone_verify'
    PURPOSE_CHOICES = [
        (PURPOSE_VENDOR_LOGIN, 'Seller login'),
        (PURPOSE_EMAIL_VERIFY, 'Email verification'),
        (PURPOSE_PHONE_VERIFY, 'Phone verification'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='otp_records',
    )
    otp = models.CharField(max_length=64)       # SHA-256 hex digest of the plaintext OTP
    purpose = models.CharField(
        max_length=20, choices=PURPOSE_CHOICES, default=PURPOSE_VENDOR_LOGIN,
    )
    # Where the code was sent. For phone verification this can be a new number
    # the user is switching to; it is only copied onto User.phone once the
    # code proves they control it.
    target = models.CharField(max_length=128, blank=True, default='')
    # Wrong guesses against this code. Capped in verification.py so a 5-digit
    # code cannot be brute-forced during its lifetime.
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_used', 'expires_at']),
            models.Index(fields=['user', 'purpose', 'is_used']),
        ]

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def verify(self, submitted_otp) -> bool:
        if self.is_used or self.is_expired():
            return False
        submitted_hash = self._hash(str(submitted_otp).strip())
        if hmac.compare_digest(self.otp, submitted_hash):
            self.is_used = True
            self.save(update_fields=['is_used'])
            return True
        return False

    @classmethod
    def create_for_user(cls, user, otp_value, ttl_minutes: int = 10,
                        purpose: str = PURPOSE_VENDOR_LOGIN, target: str = ''):
        """Invalidate this user's pending OTPs for `purpose`, then create a fresh one."""
        from datetime import timedelta
        cls.objects.filter(user=user, purpose=purpose, is_used=False).delete()
        return cls.objects.create(
            user=user,
            otp=cls._hash(str(otp_value)),
            purpose=purpose,
            target=target,
            expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
        )


class UserSession(models.Model):
    """One row per active login session. session_key = jti of the refresh token."""

    DEVICE_CHOICES = [
        ('desktop', 'Desktop'),
        ('mobile', 'Mobile'),
        ('tablet', 'Tablet'),
        ('unknown', 'Unknown'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        related_name='sessions',
        on_delete=models.CASCADE,
    )
    session_key = models.CharField(max_length=128, unique=True, db_index=True)
    device_type = models.CharField(max_length=10, choices=DEVICE_CHOICES, default='unknown')
    device_name = models.CharField(max_length=200, default='Unknown Device')
    browser = models.CharField(max_length=100, blank=True)
    os = models.CharField(max_length=100, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    is_vendor_session = models.BooleanField(default=False)
    last_activity = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_activity']
        indexes = [
            models.Index(fields=['user', 'is_vendor_session']),
        ]

    def __str__(self):
        return f"{self.user.email} — {self.device_name} ({self.ip_address})"






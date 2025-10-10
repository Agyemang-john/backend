from rest_framework import serializers
from django.core.cache import cache
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db.models import F
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.core.cache import cache
from django.core.mail import send_mail
from django.conf import settings
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone
from django.db.models import F
from .models import User
from datetime import timedelta
import random

from .models import User
from .tasks import send_otp  # assuming you use Celery for OTP sending
class OTPTokenGenerator:
    token_ttl = timedelta(minutes=10)

    def generate_otp(self):
        return random.randint(10000, 99999)

    def _is_token_expired(self, timestamp):
        expiration_time = self._num_minutes(self.token_ttl)
        return timezone.now() > (timestamp + timedelta(minutes=expiration_time))

    def _num_minutes(self, td):
        return td.days * 24 * 60 + td.seconds // 60 + td.microseconds / 60e6

otp_token_generator = OTPTokenGenerator()


MAX_FAILED_ATTEMPTS = 5
LOCKOUT_TIME = timedelta(minutes=15)


class VendorLoginSerializer(serializers.Serializer):
    email = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        identifier = attrs.get("email")
        password = attrs.get("password")

        if not identifier or not password:
            raise serializers.ValidationError("Please provide both email/phone and password.")

        is_email = "@" in identifier
        if is_email:
            try:
                validate_email(identifier)
            except ValidationError:
                raise serializers.ValidationError("Please enter a valid email address.")

        try:
            if is_email:
                user = User.objects.get(email__iexact=identifier, role="vendor")
            else:
                user = User.objects.get(phone=identifier, role="vendor")
        except User.DoesNotExist:
            raise serializers.ValidationError(
                "Vendor not registered with this email/phone."
            )

        # account lockout check
        if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            if user.lockout_until and user.lockout_until > timezone.now():
                raise serializers.ValidationError(
                    f"Too many failed attempts. Try again after {user.lockout_until.strftime('%H:%M:%S')}."
                )
            else:
                user.failed_login_attempts = 0
                user.lockout_until = None
                user.save(update_fields=["failed_login_attempts", "lockout_until"])

        if not user.is_active:
            raise serializers.ValidationError("Your account is not activated yet.")
        
        if user.role != 'vendor':
            raise serializers.ValidationError("You must be a registered seller")

        if getattr(user, "is_suspended", False):
            raise serializers.ValidationError("Your account has been suspended. Contact support.")

        authenticated_user = authenticate(email_or_phone=identifier, password=password)
        if authenticated_user is None:
            User.objects.filter(pk=user.pk).update(
                failed_login_attempts=F("failed_login_attempts") + 1
            )
            user.refresh_from_db()
            if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                user.lockout_until = timezone.now() + LOCKOUT_TIME
                user.save(update_fields=["lockout_until"])
                raise serializers.ValidationError(
                    f"Too many failed attempts. Please try again after {LOCKOUT_TIME.seconds // 60} minutes."
                )
            raise serializers.ValidationError("Incorrect password.")

        # reset attempts
        user.failed_login_attempts = 0
        user.lockout_until = None
        user.save(update_fields=["failed_login_attempts", "lockout_until"])

        # OTP generation
        otp = otp_token_generator.generate_otp()
        timestamp = timezone.now()
        cache.set(f"otp_{user.id}", {'otp': otp, 'timestamp': timestamp}, 600)

        recipient = user.email if is_email else user.phone
        send_otp.delay(recipient, otp, is_email)

        return {"detail": "OTP sent. Please verify to complete login."}

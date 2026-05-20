from rest_framework import serializers
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db.models import F
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
from .models import User
from datetime import timedelta
import random

from .tasks import send_otp
class OTPTokenGenerator:
    token_ttl = timedelta(minutes=10)

    def generate_otp(self):
        return random.randint(10000, 99999)

    def _is_token_expired(self, timestamp):
        """Accepts a Unix float (time.time()). Returns True if past token_ttl."""
        return time.time() > timestamp + self.token_ttl.total_seconds()

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

        # OTP generation — stored in DB so Redis failures never break login
        otp = otp_token_generator.generate_otp()
        from .models import OTPRecord
        OTPRecord.create_for_user(user, otp)

        recipient = user.email if is_email else user.phone
        send_otp.delay(recipient, otp, is_email)

        return {"detail": "OTP sent. Please verify to complete login."}

from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from .tokens import CustomVendorRefreshToken
class CustomTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        old_refresh = RefreshToken(attrs["refresh"])
        user_id = old_refresh.get("user_id")
        jti = old_refresh.payload.get("jti")

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise serializers.ValidationError("User not found.")

        # token_version check — catches "logout all devices"
        token_version = old_refresh.payload.get("token_version")
        if token_version is not None and token_version != user.token_version:
            raise serializers.ValidationError(
                "Your session was revoked. Please log in again."
            )

        # Session existence check (graceful migration: skip if user has no sessions yet)
        if jti:
            from .models import UserSession
            session_exists = UserSession.objects.filter(
                session_key=jti, is_vendor_session=True
            ).exists()
            if not session_exists:
                has_any = UserSession.objects.filter(
                    user_id=user_id, is_vendor_session=True
                ).exists()
                if has_any:
                    raise serializers.ValidationError(
                        "Session revoked. Please log in again."
                    )
                # Migration path: first refresh after feature deploy — create session
                from .session_utils import register_session
                register_session(user, jti, self.context["request"], is_vendor=True)
            else:
                from .session_utils import touch_session
                touch_session(jti)

        # Generate new access token with updated custom claims
        new_token = CustomVendorRefreshToken.for_user(user)
        access_token = str(new_token.access_token)

        return {"access": access_token}

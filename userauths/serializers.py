
import re
from rest_framework import serializers
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate
from django.core.mail import send_mail
from django.conf import settings
from .models import User 
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.core.validators import validate_email
from djoser.conf import settings as djoser_settings
# from djoser.serializers import PasswordResetSerializer
from django.utils import timezone
from datetime import timedelta
from django.db.models import F


class CustomPasswordResetSerializer(serializers.Serializer):
    email = serializers.EmailField()
    _user = None  # Internal variable to store the user

    def validate_email(self, value):
        user = User.objects.filter(email=value).first()

        if user:
            if not user.has_usable_password():
                self.send_social_auth_warning_email(user)
                raise serializers.ValidationError(
                    f"This account was registered using {user.auth_provider.capitalize()}. Please use that method to log in."
                )
            self._user = user

        # return value
        return value

    def get_user(self, **kwargs):
        return self._user
    
    def save(self):
        user = self.get_user()
        if user:
            context = {
                "user": user,
                "request": self.context.get("request"),
            }
            djoser_settings.EMAIL.password_reset(self.context).send(to=[user.email])

    def send_social_auth_warning_email(self, user):
        from datetime import datetime
        subject = "Password Reset Attempt on Your Social Login Account"
        context = {
            "user": user,
            "auth_provider": user.auth_provider.capitalize(),
            "now": datetime.now(),
            "login_url": settings.FRONTEND_LOGIN_URL,
        }
        html_message = render_to_string("emails/social_reset_warning.html", context)
        plain_message = strip_tags(html_message)
        from_email = settings.DEFAULT_FROM_EMAIL
        to = user.email

        send_mail(subject, plain_message, from_email, [to], html_message=html_message)


from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
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

MAX_FAILED_ATTEMPTS = 5      # max attempts before lockout
LOCKOUT_TIME = timedelta(minutes=15)  # lockout duration

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    email = serializers.CharField()  # can be email or phone
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        identifier = attrs.get("email")
        password = attrs.get("password")

        if not identifier or not password:
            raise serializers.ValidationError("Please provide both email/phone and password.")

        is_email = "@" in identifier

        # 1. If email, validate format
        if is_email:
            try:
                validate_email(identifier)
            except ValidationError:
                raise serializers.ValidationError("Please enter a valid email address.")

        # 2. Find user
        try:
            if is_email:
                user = User.objects.get(email__iexact=identifier)
            else:
                user = User.objects.get(phone=identifier)
        except User.DoesNotExist:
            raise serializers.ValidationError(
                "Email not registered." if is_email else "Phone number not registered."
            )

        # 3. Check if account is locked
        if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            if user.lockout_until and user.lockout_until > timezone.now():
                raise serializers.ValidationError(
                    f"Too many failed attempts. Try again after {user.lockout_until.strftime('%H:%M:%S')}."
                )
            else:
                # Reset if lockout expired
                user.failed_login_attempts = 0
                user.lockout_until = None
                user.save(update_fields=["failed_login_attempts", "lockout_until"])

        # 4. Check active
        if not user.is_active:
            raise serializers.ValidationError("Your account is not activated yet.")

        # 5. Check suspended
        if getattr(user, "is_suspended", False):
            raise serializers.ValidationError("Your account has been suspended. Contact support.")

        # 6. Authenticate credentials
        authenticated_user = authenticate(email_or_phone=identifier, password=password)
        if authenticated_user is None:
            # Increment failed login counter on the actual user object
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

        user.failed_login_attempts = 0
        user.lockout_until = None
        user.save(update_fields=["failed_login_attempts", "lockout_until"])
        # For non-vendors (e.g., customers), issue tokens with custom claims
        refresh = RefreshToken.for_user(user)
        refresh["role"] = user.role
        refresh["is_active"] = user.is_active
        refresh["is_staff"] = user.is_staff
        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }

from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenRefreshSerializer

class CustomerCustomTokenRefreshSerializer(TokenRefreshSerializer):
    refresh = serializers.CharField(required=False, write_only=True)

    def validate(self, attrs):
        # 1. Get token from cookie if not in body
        refresh_token = attrs.get("refresh") or self.context["request"].COOKIES.get("refresh")
        if not refresh_token:
            raise serializers.ValidationError("No refresh token found")

        attrs["refresh"] = refresh_token
        data = super().validate(attrs)

        refresh = getattr(self, "token", None)
        if not refresh:
            from rest_framework_simplejwt.tokens import RefreshToken
            refresh = RefreshToken(refresh_token)

        # use original token as fallback

        access = refresh.access_token

        # 4. Add your custom claims
        user_id = refresh.payload.get("user_id")
        if user_id:
            try:
                user = User.objects.only("role", "is_active", "is_staff").get(id=user_id)
                access["role"] = user.role or "customer"
                access["is_active"] = user.is_active
                access["is_staff"] = user.is_staff
            except User.DoesNotExist:
                pass

        data["access"] = str(access)
        return data

import re
from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone", "password"]

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists. Login or reset password")
        return value

    def validate_phone(self, value):
        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("An account with this phone number already exists. Login or reset password")
        return value

    def validate_password(self, value):
        """
        Strong password validation:
        - >= 8 chars
        - uppercase
        - lowercase
        - number
        - special char
        """
        if len(value) < 8:
            raise serializers.ValidationError("Password must be at least 8 characters long.")
        if not re.search(r"[A-Z]", value):
            raise serializers.ValidationError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", value):
            raise serializers.ValidationError("Password must contain at least one lowercase letter.")
        if not re.search(r"\d", value):
            raise serializers.ValidationError("Password must contain at least one number.")
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", value):
            raise serializers.ValidationError("Password must contain at least one special character.")
        
        try:
            validate_password(value)
        except ValidationError as e:
            raise serializers.ValidationError(str(e))
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")

        # user starts inactive until email is verified
        user = User.objects.create(
            is_active=False,
            **validated_data
        )
        user.set_password(password)
        user.save()

        # send verification email BUT never break request
        from .utils import send_activation_email_safe
        send_activation_email_safe(user)
        return user


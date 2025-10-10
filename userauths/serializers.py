
from .models import User
import re
from rest_framework import serializers
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.contrib.auth import authenticate
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import smart_str, smart_bytes
from django.core.mail import send_mail
from django.conf import settings
from .models import User 
from .utils import send_password_reset_email
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from django.db.models import Q
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.core.validators import validate_email
from djoser.conf import settings as djoser_settings
# from djoser.serializers import PasswordResetSerializer
from django.utils import timezone
from datetime import timedelta
from django.db.models import F
from .tasks import send_otp


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
import six
from django.utils.crypto import constant_time_compare
from django.utils.http import base36_to_int, int_to_base36
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
        refresh["is_admin"] = user.is_admin
        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }


from rest_framework_simplejwt.serializers import TokenRefreshSerializer

class CustomTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)

        refresh = RefreshToken(attrs["refresh"])
        user = self.context["request"].user or None

        if user and user.is_authenticated:
            # add role claims
            new_access = refresh.access_token
            new_access["role"] = user.role
            refresh["is_active"] = user.is_active
            new_access["is_admin"] = user.is_admin
            data["access"] = str(new_access)

        return data
    

class UserRegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(max_length=70, min_length=8, write_only=True)

    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name', 'phone', 'password']
    
    def validate(self, attrs):
        password = attrs.get('password')
        
        # Custom password rules
        if not any(char.isupper() for char in password):
            raise serializers.ValidationError("Password must contain at least one uppercase letter.")
        if not any(char.islower() for char in password):
            raise serializers.ValidationError("Password must contain at least one lowercase letter.")
        if not any(char.isdigit() for char in password):
            raise serializers.ValidationError("Password must contain at least one number.")
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            raise serializers.ValidationError("Password must contain at least one special character.")
        
        # Additional Django password validation (optional)
        try:
            validate_password(password)
        except ValidationError as e:
            raise serializers.ValidationError(str(e))
        
        # Check for duplicate email or phone number
        email = attrs.get('email')
        phone = attrs.get('phone')

        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("An account with this email already exists. Login or reset password")
        
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError("An account with this phone number already exists. Login or reset password")
        
        return super().validate(attrs)

    def create(self, validated_data):
        # Create user and hash the password
        user = User.objects.create_user(**validated_data)
        return user

class LoginSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(max_length=255, min_length=6)
    password = serializers.CharField(max_length=68, write_only=True)
    refresh_token = serializers.CharField(max_length=300, read_only=True)
    access_token = serializers.CharField(max_length=300, read_only=True)
    first_name = serializers.CharField(max_length=30, read_only=True)

    class Meta:
        model = User
        fields = ['email', 'password', 'access_token', 'refresh_token', 'first_name']

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')
        request = self.context.get('request')

        # Authenticate user
        user = authenticate(request, email=email, password=password)

        if not user:
            raise AuthenticationFailed('Invalid credentials, please try again.')
        if not user.is_active:
            raise AuthenticationFailed('Account is not active. Please verify your email.')

        # Get tokens for authenticated user
        user_tokens = user.tokens()

        return {
            'email': user.email,
            'first_name': user.first_name,
            'access_token': user_tokens['access'],
            'refresh_token': user_tokens['refresh'],
        }


class TokenRefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()

    def validate(self, attrs):
        refresh_token = attrs.get('refresh')

        if not refresh_token:
            raise serializers.ValidationError("Refresh token is required.")

        try:
            refresh = RefreshToken(refresh_token)
            attrs['access'] = str(refresh.access_token)
        except TokenError as e:
            raise serializers.ValidationError("Invalid or expired refresh token.")
        
        return attrs


class PasswordResestRequestSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(max_length=50)

    class Meta:
        model = User
        fields = ['email']

    def validate(self, attrs):
        email = attrs.get('email')

        try:
            user = User.objects.get(email=email)
            uidb64 = urlsafe_base64_encode(smart_bytes(user.id))
            token = PasswordResetTokenGenerator().make_token(user)
            request = self.context.get('request')
            protocol = 'https' if request.is_secure() else 'http'

            # site_domain = '192.168.102.89:5173/'
            # # site_domain = get_current_site(request).domain
            # relative_link = reverse('userauths:password-reset-confirm', kwargs={'uidb64': uidb64, 'token': token})
            # reset_link = f'{protocol}://{site_domain}{relative_link}'

            frontend_reset_url = 'http://192.168.102.89:5173/auth/reset-password'  # Adjust to your actual frontend URL
            reset_link = f'{frontend_reset_url}?uidb64={uidb64}&token={token}'

            # Send the reset email
            if not send_password_reset_email(email=email, reset_link=reset_link, user_name=user.first_name):
                raise serializers.ValidationError("Failed to send the password reset email.")

        except User.DoesNotExist:
            raise serializers.ValidationError("User with this email does not exist.")

        return super().validate(attrs)

class PasswordResetConfirmSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)
    confirm_password = serializers.CharField(write_only=True, min_length=8, max_length=128)

    class Meta:
        model = User
        fields = ['password', 'confirm_password']

    def validate(self, attrs):
        password = attrs.get('password')
        confirm_password = attrs.get('confirm_password')

        try:
            validate_password(password)
        except ValidationError as e:
            raise serializers.ValidationError(str(e))

        if password != confirm_password:
            raise serializers.ValidationError("Passwords do not match.")
        return attrs

class LogOutSerializer(serializers.Serializer):
    refresh_token = serializers.CharField(max_length=300)

    default_error_messages = {
        'bad_token': "Invalid or expired token.",
    }

    def validate(self, attrs):
        # Validate the presence of the refresh token
        self.token = attrs.get('refresh_token')
        if not self.token:
            raise serializers.ValidationError("Refresh token is required.")
        return attrs

    def save(self, **kwargs):
        try:
            # Attempt to blacklist the token
            token = RefreshToken(self.token)
            token.blacklist()
        except TokenError:
            # Handle invalid or expired token errors
            self.fail('bad_token')
        return {"success": "Logged out successfully."}
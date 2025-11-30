from django.contrib.sites.shortcuts import get_current_site
from django.template.loader import render_to_string
from django.core.mail import EmailMessage
from ecommerce import settings
from django.core.cache import cache
from .tasks import send_activation_email_task
from  .otp import cache_activation_data

# from twilio.rest import Client
import pyotp

def generate_otp(secret_key=None, interval=300):
    """
    Generate a time-based OTP using pyotp.
    """
    if not secret_key:
        secret_key = pyotp.random_base32()  # Generate a random base32 secret key
    totp = pyotp.TOTP(secret_key, interval=interval)
    otp = totp.now()  # Generate a current OTP
    return otp, secret_key


def send_email_otp(to_email, otp, user_name, request):
    """
    Send a styled OTP email using an HTML template.
    """

    cache_key = f"otp_{to_email}"
    cache.set(cache_key, otp, timeout=300)  

    subject = "Your OTP Code"
    
    # Render the HTML template
    context = {
        'otp': otp,
        'user_name': user_name,
        'support_email': get_current_site(request).domain,
    }
    html_content = render_to_string('email/otp-email.html', context)
    
    # Create the email message
    email = EmailMessage(
        subject=subject,
        body=html_content,
        from_email=settings.EMAIL_HOST_USER,
        to=[to_email],
    )
    email.content_subtype = "html"  # Specify the email content as HTML

    try:
        email.send()
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

def send_activation_email_safe(user):
    from django.conf import settings
    activation_data = cache_activation_data(user)
    uid = activation_data["uid"]
    token = activation_data["email_token"]

    activation_link = f"{settings.SITE_URL}/auth/activation/{uid}/{token}"

    user_data = {
        "first_name": user.first_name,
        "email": user.email,
    }

    # Try sending email asynchronously (safe)
    try:
        send_activation_email_task.delay(user_data, activation_link)
    except:
        # Absolutely NEVER break registration
        pass

    return activation_data

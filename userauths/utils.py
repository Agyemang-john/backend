from django.core.exceptions import PermissionDenied
from django.contrib.sites.shortcuts import get_current_site
from django.template.loader import render_to_string
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMessage, send_mail
from ecommerce import settings
from .tokens import account_activation_token
from django.shortcuts import redirect
from django.core.cache import cache

# from twilio.rest import Client
import logging
import requests
import json
from django.views.decorators.csrf import csrf_exempt
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


def send_password_reset_email(email, reset_link, user_name):
    """
    Sends a styled password reset email to the user.
    :param email: User's email address
    :param reset_link: Password reset link
    :param user_name: User's name
    """
    subject = "Password Reset Request"
    
    # Render the HTML template
    context = {
        'reset_link': reset_link,
        'user_name': user_name,
        'support_email': settings.EMAIL_HOST_USER,
    }
    html_content = render_to_string('email/password-reset.html', context)
    
    # Create the email message
    email_message = EmailMessage(
        subject=subject,
        body=html_content,
        from_email=settings.EMAIL_HOST_USER,
        to=[email],
    )
    email_message.content_subtype = "html"  # Specify that the email content is HTML

    try:
        email_message.send()
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False


# logger = logging.getLogger(__name__)
# @csrf_exempt
def send_sms(phone_number, message):
    url = "https://sms.mhiskall.tech/sendsms/"
    apikey = '74APBFK2ZEYKHF8'
    sender_id = 'ADEPAMALL'
    urll = 'https://sms.mhiskall.tech/sendsms?apikey={}&from={}&to={}&message={}'.format(apikey,sender_id,phone_number,message)

    # headers = {
    #     'Authorization': f"Bearer {apikey}",  # Add a space after "Bearer"
    #     'Content-Type': 'application/json'
    # }
    # payload = json.dumps({
    #     'sender': sender_id,
    #     'to': phone_number,
    #     "message": message
    # })
    
    response = requests.request('GET',urll)
    return response.json()



def detect_user(user):
    if user.role is None and user.is_superadmin:
        return '/secret'
    
    role_dashboard = {
        'vendor': 'userauths:vendor-dashboard',
        'customer': 'customer:dashboard',
    }
    return role_dashboard.get(user.role)


def is_customer(user):
    """Returns True if the user us is customer"""
    if user.role == 'customer':
        return True
    raise PermissionDenied


def is_vendor(user):
    """Returns True if the user us is vendor"""
    if user.role == 'vendor':
        return True
    raise PermissionDenied


def send_email(request, mail_subject, email_template, user):
    """Send email"""
    from_email = settings.EMAIL_HOST_USER
    message = render_to_string(email_template, {
        'user': user.username,
        'domain': get_current_site(request).domain,
        'uid': urlsafe_base64_encode(force_bytes(user.pk)),
        'token': account_activation_token.make_token(user),
        "protocol": 'https' if request.is_secure() else 'http'
    })
    to_email = user.email

    # -----------------------------------------------------------------------------------
    # mail = EmailMessage(mail_subject, message, from_email, to=[to_email])
    # mail.content_subtype = 'html'
    # mail.send()
    print(f'Subject: {mail_subject}\nFrom: {from_email}\nTo: {to_email}\n{message}')
    # -----------------------------------------------------------------------------------


def send_notification_email(mail_subject, mail_template, context, request):
    """Send notification email"""
    from_email = settings.EMAIL_HOST_USER
    # message = render_to_string(mail_template, context)
    if isinstance(context['to_email'], str):
        to_email = [context['to_email']]
    else:
        to_email = context['to_email']

    subject = "Congratulations! Your shop has been approved"
    message = render_to_string("email/admin-approval-email.html", {
        'domain': get_current_site(request).domain,
        "protocol": 'https' if request.is_secure() else 'http'
    })
    email = send_mail(subject, message,  to=to_email)
     # -----------------------------------------------------------------------------------
    # mail = EmailMessage(mail_subject, message, from_email, to=to_email)
    # mail.content_subtype = 'html'
    # mail.send()
    print(f'Subject: {mail_subject}\nFrom: {from_email}\nTo: {to_email}\n{message}')
    # -----------------------------------------------------------------------------------

# def send_sms(phone_number, message):
#     client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    
#     try:
#         message = client.messages.create(
#             body=message,
#             from_=settings.TWILIO_PHONE_NUMBER,
#             to=phone_number
#         )
#         return message.sid  # or any other relevant information
#     except Exception as e:
#         # Log the exception if needed
#         print(f"Failed to send SMS: {e}")
#         return None
from celery import shared_task
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
import logging

logger = logging.getLogger('otp')

_sms_client = None

def _get_sms_client():
    global _sms_client
    if _sms_client is None:
        from userauths.arkesel_client import ArkeselSMS
        _sms_client = ArkeselSMS()
    return _sms_client


@shared_task(max_retries=3, retry_backoff=True)
def send_otp(recipient, otp, is_email=True):
    context = {
        'otp': otp,
        'brand_name': 'Negromart',
        'expiry_minutes': 10,
        'logo_url': 'https://seller.negromart.com/favicon.png',
    }

    if is_email:
        try:
            subject = 'Negromart seller Login OTP'
            html_message = render_to_string('email/otp_email.html', context)
            plain_message = f'Your OTP is: {otp}. It expires in 10 minutes.'
            send_mail(
                subject=subject,
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                html_message=html_message,
                fail_silently=False,
            )
            logger.info(f"OTP email sent to {recipient}")
        except Exception as e:
            logger.error(f"Failed to send OTP email to {recipient}: {str(e)}")
            raise
    else:
        try:
            response = _get_sms_client().send_sms(
                sender=settings.ARKESEL_SENDER,
                message=f'Your OTP is: {otp}. It expires in 10 minutes.',
                recipients=[recipient],
            )
            if response.get('status') == 'success':
                logger.info(f"OTP SMS sent to {recipient}: {response}")
            else:
                logger.error(f"Failed to send OTP SMS to {recipient}: {response}")
                raise Exception(f"Arkesel API error: {response.get('message', 'Unknown error')}")
        except Exception as e:
            logger.error(f"SMS sending failed for {recipient}: {str(e)}")
            raise


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_verification_code(self, recipient, code, channel, first_name=''):
    """
    Deliver an email/phone verification code (userauths/verification.py).

    Separate from send_otp because the wording differs: this confirms that a
    person owns an address, it does not log them in to the seller dashboard.
    """
    try:
        if channel == 'email':
            context = {
                'code': code,
                'first_name': first_name,
                'brand_name': 'Negromart',
                'expiry_minutes': 10,
                'logo_url': 'https://seller.negromart.com/favicon.png',
            }
            html_message = render_to_string('email/verification_code.html', context)
            send_mail(
                subject=f'{code} is your Negromart verification code',
                message=(
                    f'Your Negromart verification code is {code}. It expires in 10 minutes. '
                    'If you did not request it, you can ignore this email.'
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                html_message=html_message,
                fail_silently=False,
            )
        else:
            response = _get_sms_client().send_sms(
                sender=settings.ARKESEL_SENDER,
                message=f'{code} is your Negromart verification code. It expires in 10 minutes. Never share it.',
                recipients=[recipient],
            )
            if response.get('status') != 'success':
                raise Exception(f"Arkesel API error: {response.get('message', 'Unknown error')}")
        logger.info("verification code sent via %s", channel)
    except Exception as exc:
        logger.error("verification code via %s failed: %s", channel, exc)
        raise self.retry(exc=exc)


@shared_task(ignore_result=True)
def cleanup_expired_otps():
    """
    Delete OTPRecord rows that are expired or already used.
    Keeps the table small. Runs every 30 minutes via Celery Beat.
    """
    from django.utils import timezone
    from userauths.models import OTPRecord
    deleted, _ = OTPRecord.objects.filter(
        expires_at__lt=timezone.now()
    ).delete()
    used_deleted, _ = OTPRecord.objects.filter(is_used=True).delete()
    total = deleted + used_deleted
    if total:
        logger.info("cleanup_expired_otps: removed %d stale OTP records", total)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_activation_email_task(self, user_data, activation_link):
    subject = "Activate Your Account"
    from_email = settings.DEFAULT_FROM_EMAIL
    to_email = user_data["email"]

    # Render the HTML template
    html_content = render_to_string("email/activation_email.html", {
        "first_name": user_data["first_name"],
        "activation_link": activation_link,
    })

    # Fallback plain text
    text_content = f"""
    Hello {user_data['first_name']},

    Please activate your account by clicking the link below:
    {activation_link}

    If you did not register, ignore this message.
    """

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
    except Exception as exc:
        return self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_new_sign_in_alert(self, event_id):
    """Email a seller when their account signs in from a device it never used before."""
    from django.utils import timezone
    from .models import SecurityEvent
    try:
        event = SecurityEvent.objects.select_related('user').get(pk=event_id)
    except SecurityEvent.DoesNotExist:
        return
    user = event.user
    seller_url = getattr(settings, 'SELLER_SITE_URL', 'https://seller.negromart.com').rstrip('/')
    context = {
        'first_name': user.first_name,
        'device_name': event.device_name or 'Unknown device',
        'ip_address': event.ip_address or 'Unknown',
        'signed_in_at': timezone.localtime(event.created_at).strftime('%d %b %Y, %H:%M %Z'),
        'security_url': f'{seller_url}/settings?tab=security',
    }
    html = render_to_string('email/new-sign-in-alert.html', context)
    text = (
        f"Hello {user.first_name},\n\n"
        f"Your Negromart seller account was just signed in to from a new device.\n\n"
        f"Device: {context['device_name']}\nIP address: {context['ip_address']}\n"
        f"Time: {context['signed_in_at']}\n\n"
        f"If this was you, there's nothing to do.\n"
        f"If it wasn't, open {context['security_url']}, sign out everywhere and change your password."
    )
    try:
        msg = EmailMultiAlternatives(
            'New sign-in to your Negromart seller account', text,
            settings.DEFAULT_FROM_EMAIL, [user.email],
        )
        msg.attach_alternative(html, 'text/html')
        msg.send(fail_silently=False)
    except Exception as exc:
        return self.retry(exc=exc)

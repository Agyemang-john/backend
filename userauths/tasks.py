from celery import shared_task
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings
import logging
# from arkesel_python import ArkeselSMS
from userauths.arkesel_client import ArkeselSMS
sms_client = ArkeselSMS()

logger = logging.getLogger('otp')


@shared_task(max_retries=3, retry_backoff=True)
def send_otp(recipient, otp, is_email=True):
    """
    Send OTP via email or SMS using Arkesel.
    :param recipient: Email address or phone number
    :param otp: One-time password
    :param is_email: True for email, False for SMS
    """
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
            response = sms_client.send_sms(
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


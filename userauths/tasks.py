from celery import shared_task
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
import logging
# from arkesel_python import ArkeselSMS
from userauths.arkesel_client import ArkeselSMS
sms_client = ArkeselSMS()

logger = logging.getLogger('otp')


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



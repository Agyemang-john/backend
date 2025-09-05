from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings

def send_subscription_email(user_email, token):
    confirm_url = f"{settings.SITE_URL}/newsletter/confirm/{token}/"

    subject = "Confirm Your Subscription"
    message = render_to_string("email/newsletter_confirmation.html", {
        "confirm_url": confirm_url,
    })

    email = EmailMessage(
        subject=subject,
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user_email],
    )
    email.content_subtype = "html"  # tell Django it's HTML
    email.send()

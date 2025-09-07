# tasks.py
from celery import shared_task
from django.conf import settings
from .models import Order

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.utils import timezone

@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def send_order_email_to_sellers(self, order_id):
    try:
        order = Order.objects.get(id=order_id)
        vendors = order.vendors.all()

        emails_sent = 0
        for vendor in vendors:
            subject = f"New Order #{order.order_number}"

            context = {
                "vendor": vendor,
                "order": order,
                "site_name": "Negromart",
                "site_logo_url": f"{settings.SITE_URL}/favicon.png",
                "dashboard_url": f"{settings.SITE_URL}/seller/orders/{order.id}/",
                "year": timezone.now().year,
            }

            html_message = render_to_string("email/order_notification.html", context)
            plain_message = strip_tags(html_message)

            email = EmailMultiAlternatives(
                subject=subject,
                body=plain_message,  # fallback for plain-text clients
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[vendor.email],
            )
            email.attach_alternative(html_message, "text/html")
            email.send(fail_silently=False)
            emails_sent += 1

        return {"order_id": order_id, "emails_sent": emails_sent}

    except Order.DoesNotExist:
        return {"order_id": order_id, "emails_sent": 0}

    except Exception as exc:
        raise self.retry(exc=exc)

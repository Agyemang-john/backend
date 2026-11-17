# tasks.py
from celery import shared_task
from django.conf import settings
from .models import Order

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
import logging

# Set up logging
logger = logging.getLogger(__name__)

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
                "dashboard_url": f"https://seller.negromart.com/orders",
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


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def send_order_email_to_customer(self, order_id):
    try:
        order = Order.objects.get(id=order_id)
        address = order.address
        
        if not address.email:
            logger.warning(f"No email found for address ID {address.id} in order {order_id}")
            return {"order_id": order_id, "email_sent": False, "error": "No customer email provided"}

        subject = f"Order Confirmation #{order.order_number}"

        context = {
            "customer_name": address.full_name or "Valued Customer",
            "order": order,
            "site_name": "Negromart",
            "site_logo_url": f"{settings.SITE_URL}/favicon.png",
            "order_url": f"{settings.SITE_URL}/dashboard/order-history/{order.id}",
            "year": timezone.now().year,
        }

        html_message = render_to_string("email/customer_order_confirmation.html", context)
        plain_message = strip_tags(html_message)

        email = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[address.email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)

        return {"order_id": order_id, "email_sent": True}

    except Order.DoesNotExist:
        logger.error(f"Order {order_id} not found")
        return {"order_id": order_id, "email_sent": False, "error": "Order not found"}

    except Exception as exc:
        logger.error(f"Error sending customer email for order {order_id}: {str(exc)}")
        raise self.retry(exc=exc)


@shared_task(ignore_result=True)
def check_sourced_orders():
    """
    Daily: send each store a digest of imported / made-to-order items that are
    late, overdue or due soon (bell daily, email when one gets worse), and tell
    buyers when a late one can be cancelled (order/sourcing.py).
    """
    from .sourcing import notify_overdue, remind_sellers
    stores = remind_sellers()
    buyers = notify_overdue()
    logger.info("check_sourced_orders: reminded %s store(s), %s late line(s) now cancellable", stores, buyers)


@shared_task(ignore_result=True)
def sync_courier_tracking(batch=200):
    """
    Every 30 minutes: ask each courier that can be polled (Raven) where its
    open shipments are, and apply anything its webhooks never delivered. A
    courier gives up on a webhook after a few hours of retries, so without
    this a short outage here leaves a parcel stuck at its last status.
    Oldest-checked first; updated_at is touched so the next run moves on.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .delivery import DeliveryProviderError, available_providers
    from .fulfilment import TERMINAL_SHIPMENT_STATUSES, apply_courier_updates
    from .models import Shipment

    now = timezone.now()
    for provider in available_providers():
        if not provider.supports_tracking_sync:
            continue
        shipments = list(
            Shipment.objects.filter(provider=provider.code, created_at__lte=now - timedelta(minutes=10))
            .exclude(status__in=TERMINAL_SHIPMENT_STATUSES).exclude(external_reference='')
            .order_by('updated_at')[:batch]
        )
        checked = applied = failed = 0
        for shipment in shipments:
            try:
                updates = provider.sync(shipment)
            except DeliveryProviderError as exc:
                failed += 1
                logger.warning("sync_courier_tracking: %s %s: %s", provider.code, shipment.shipment_id, exc)
                continue
            except Exception:
                failed += 1
                logger.exception("sync_courier_tracking: %s %s failed", provider.code, shipment.shipment_id)
                continue
            checked += 1
            applied += apply_courier_updates(provider.code, updates)
            Shipment.objects.filter(pk=shipment.pk).update(updated_at=timezone.now())
        if shipments:
            logger.info("sync_courier_tracking: %s checked=%s applied=%s failed=%s",
                        provider.code, checked, applied, failed)

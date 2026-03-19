from celery import shared_task
from order.models import *
from product.models import *
from django.utils.crypto import get_random_string
from userauths.models import User
from celery.utils.log import get_task_logger
from .payout_service import PayoutService
from decimal import Decimal
# order/tasks.py
from django.contrib.contenttypes.models import ContentType
from notification.models import Notification
from address.models import Address

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def create_order_from_payment_task(
    self,
    user_id,
    payment_data,
    payment_id,
    cart_items_data,
    address_id,
    ip,
    reference
):
    try:
        user = User.objects.get(id=user_id)
        address = Address.objects.get(id=address_id)
        payment_amount = payment_data["amount"] / 100

        # Create Order
        order = Order.objects.create(
            user=user,
            total=payment_amount,
            payment_method='paystack',
            payment_id=payment_id,
            status="pending",
            address=address,
            ip=ip,
            is_ordered=True,
        )

        # Assign vendors
        unique_vendors = set()
        order_products = []

        for item_data in cart_items_data:
            product = Product.objects.get(id=item_data["product_id"])
            variant = Variants.objects.get(id=item_data["variant_id"]) if item_data["variant_id"] else None

            if product.vendor:
                unique_vendors.add(product.vendor)

            price = variant.price if variant else product.price

            order_products.append(OrderProduct(
                order=order,
                product=product,
                variant=variant,
                quantity=item_data["quantity"],
                price=price,
                amount=price * item_data["quantity"],
                selected_delivery_option_id=item_data["delivery_option_id"],
            ))

            # Update stock deduction
            if variant:
                variant.quantity -= item_data["quantity"]
                variant.full_clean()
                variant.save()
            else:
                product.total_quantity -= item_data["quantity"]
                product.full_clean()
                product.save()

        # Bulk create order products
        OrderProduct.objects.bulk_create(order_products)
        order.vendors.set(unique_vendors)

        # Generate unique order number
        while True:
            order_number = f"INVOICE_NO-{get_random_string(8).upper()}"
            if not Order.objects.filter(order_number=order_number).exists():
                break

        order.order_number = order_number
        order.save()

        # Send notifications to vendors
        order_ct = ContentType.objects.get_for_model(Order)
        for vendor in unique_vendors:
            if hasattr(vendor, 'user') and vendor.user:
                Notification.objects.create(
                    recipient=vendor.user,  # This is correct — send to the actual User
                    verb="vendor_new_order",
                    actor=user,
                    target=order,
                    data={
                        "order_number": order.order_number,
                        "total_amount": f"GHS {order.total:,.2f}",
                        "items_count": len(order_products),
                        "buyer_name": user.first_name or user.first_name,
                        "message": f"New order received! #{order.order_number}",
                        "url": f"/vendor/orders/{order.id}/"
                    }
                )
            else:
                logger.warning(f"Vendor {vendor.name} has no linked user account. Notification skipped.")

        # Clear user's cart (safe now)
        CartItem.objects.filter(cart__user=user).delete()

        logger.info(f"Order {order.order_number} created successfully for user {user.id}")

    except Exception as exc:
        logger.error(f"Failed to create order for payment {reference}: {exc}", exc_info=True)
        # Optional: send admin alert, mark payment as suspicious, etc.
        raise self.retry(exc=exc)

# from celery import shared_task
# from django.db import transaction
# from django.utils import timezone
# import logging

# logger = logging.getLogger(__name__)

# @shared_task(bind=True, max_retries=3, default_retry_delay=60)
# def create_order_and_shipments_task(
#     self,
#     user_id,
#     payment_data,
#     payment_id,
#     cart_items_data,
#     address_id,
#     ip,
#     reference
# ):
#     try:
#         with transaction.atomic():
#             user = User.objects.get(id=user_id)
#             address = Address.objects.get(id=address_id)
#             payment_amount = payment_data["amount"] / 100  # Paystack sends in kobo

#             # 1. Create main Order
#             order = Order.objects.create(
#                 user=user,
#                 order_number="",  # Will generate later
#                 total=payment_amount,
#                 payment_method='paystack',
#                 payment_id=str(payment_id),
#                 address=address,
#                 ip=ip or "",
#                 is_ordered=True,
#                 response_date=timezone.now(),
#             )

#             # Generate unique order number
#             from django.utils.crypto import get_random_string
#             while True:
#                 order_number = f"ORD-{timezone.now().strftime('%Y%m%d')}-{get_random_string(6).upper()}"
#                 if not Order.objects.filter(order_number=order_number).exists():
#                     order.order_number = order_number
#                     order.save()
#                     break

#             order_products = []
#             vendor_groups = {}  # vendor_id → list of order_products

#             # 2. Create OrderProducts + group by vendor
#             for item_data in cart_items_data:
#                 product = Product.objects.select_related('vendor').get(id=item_data["product_id"])
#                 variant = Variants.objects.get(id=item_data["variant_id"]) if item_data["variant_id"] else None
#                 delivery_option = DeliveryOption.objects.get(id=item_data["delivery_option_id"]) if item_data["delivery_option_id"] else None

#                 price = variant.price if variant else product.price
#                 quantity = item_data["quantity"]

#                 order_product = OrderProduct(
#                     order=order,
#                     product=product,
#                     variant=variant,
#                     quantity=quantity,
#                     price=price,
#                     amount=price * quantity,
#                     selected_delivery_option=delivery_option,
#                 )
#                 order_products.append(order_product)

#                 # Group by vendor
#                 vendor = product.vendor
#                 if vendor not in vendor_groups:
#                     vendor_groups[vendor] = []
#                 vendor_groups[vendor].append(order_product)

#                 # Stock deduction (with lock to prevent overselling)
#                 if variant:
#                     obj = Variants.objects.select_for_update().get(id=variant.id)
#                     if obj.quantity < item_data["quantity"]:
#                         raise ValueError(f"Only {obj.quantity} left for {variant}")
#                     obj.quantity -= item_data["quantity"]
#                     obj.save()
#                 else:
#                     obj = Product.objects.select_for_update().get(id=product.id)
#                     if obj.total_quantity < item_data["quantity"]:
#                         raise ValueError(f"Only {obj.total_quantity} left for {product.title}")
#                     obj.total_quantity -= item_data["quantity"]
#                     obj.save()

#             # Bulk create all OrderProducts
#             OrderProduct.objects.bulk_create(order_products)

#             # 3. Create one Shipment per Vendor
#             shipments = []
#             for vendor, op_list in vendor_groups.items():
#                 is_international = address.country != vendor.shipping_from_country.name if vendor.shipping_from_country and hasattr(address, 'country') else False

#                 shipment = Shipment.objects.create(
#                     order=order,
#                     vendor=vendor,
#                     status='pending',
#                     is_international=is_international,
#                     estimated_delivery_date=None,  # You can calculate from delivery_option
#                 )

#                 # Assign items to shipment
#                 shipment.items.set(op_list)
#                 shipments.append(shipment)

#                 # Optional: Auto-set estimated delivery
#                 if op_list:
#                     sample_op = op_list[0]
#                     if sample_op.selected_delivery_option:
#                         delivery_range = sample_op.get_delivery_range()
#                         if delivery_range and "to" in delivery_range:
#                             try:
#                                 date_str = delivery_range.split(" to ")[-1]
#                                 from dateutil.parser import parse
#                                 shipment.estimated_delivery_date = parse(date_str).date()
#                                 shipment.save()
#                             except:
#                                 pass

#             # 4. Assign vendors to order
#             order.vendors.set(vendor_groups.keys())

#             # 5. Clear cart
#             CartItem.objects.filter(cart__user=user).delete()

#             logger.info(f"Order {order.order_number} created with {len(shipments)} shipment(s)")
            
#     except Exception as exc:
#         logger.error(f"Order creation failed for ref {reference}: {exc}", exc_info=True)
#         raise self.retry(exc=exc)


# Payout Task
@shared_task
def batch_payouts():
    """Celery task to process payouts for all vendors every 2 days."""
    logger.info("Starting batch payout process")
    vendors = Vendor.objects.filter(payment_methods__payment_method='momo', payment_methods__status='verified').distinct()
    
    for vendor in vendors:
        # Get completed orders (delivered) not yet paid out
        orders = Order.objects.filter(
            vendors=vendor,
            status='delivered',
            payouts__isnull=True
        )
        if not orders.exists():
            logger.info(f"No eligible orders for vendor {vendor.id}")
            continue

        # Calculate total amount (80% of vendor's share as an example)
        total_amount = sum(Decimal(str(order.get_vendor_total(vendor))) * Decimal('0.8') for order in orders)
        if total_amount <= 0:
            logger.info(f"No positive amount to pay for vendor {vendor.id}")
            continue

        logger.info(f"Processing payout of {total_amount} GHS for vendor {vendor.id}")
        payout_service = PayoutService()
        result = payout_service.process_vendor_payout(vendor, orders, total_amount)
        
        if result["status"] == "success":
            logger.info(f"Payout successful for vendor {vendor.id}: {result['transaction_id']}")
        else:
            logger.error(f"Payout failed for vendor {vendor.id}: {result['message']}")


# subscriptions/tasks.py

import logging
from celery import shared_task
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Daily renewal processor — runs at 8am via Celery Beat
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="subscriptions.process_renewals")
def process_subscription_renewals():
    """
    Finds all subscriptions expiring tomorrow with auto_renew=True
    and queues an individual charge task for each.

    Celery Beat schedule (add to settings.py):

    CELERY_BEAT_SCHEDULE = {
        'process-renewals-daily': {
            'task': 'subscriptions.process_renewals',
            'schedule': crontab(hour=8, minute=0),
        },
        'expire-old-subscriptions': {
            'task': 'subscriptions.expire_old_subscriptions',
            'schedule': crontab(hour=0, minute=30),
        },
        'warn-expiring-soon': {
            'task': 'subscriptions.warn_expiring_soon',
            'schedule': crontab(hour=9, minute=0),
        },
    }
    """
    from .models import VendorSubscription

    tomorrow = timezone.now() + timedelta(days=1)

    due = VendorSubscription.objects.filter(
        status='active',
        auto_renew=True,
        end_date__date=tomorrow.date(),
    ).values_list('id', flat=True)

    count = 0
    for sub_id in due:
        charge_vendor_for_renewal.delay(sub_id)
        count += 1

    logger.info(f"process_subscription_renewals: queued {count} renewal tasks")
    return f"queued:{count}"


@shared_task(
    name="subscriptions.charge_vendor_for_renewal",
    bind=True,
    max_retries=3,
    default_retry_delay=86400,      # Retry after 24 hours
)
def charge_vendor_for_renewal(self, subscription_id: int):
    """
    Charges a single vendor's saved card for renewal.
    Retries up to 3 times (once per day) before expiring the subscription.
    """
    from . import services
    from .models import VendorSubscription

    try:
        result = services.charge_for_renewal(subscription_id)
        logger.info(f"Renewal success: sub_id={subscription_id}, ref={result.get('reference')}")
        return result

    except Exception as exc:
        logger.warning(
            f"Renewal attempt {self.request.retries + 1}/3 failed for sub_id={subscription_id}: {exc}"
        )

        if self.request.retries < self.max_retries - 1:
            raise self.retry(exc=exc)
        else:
            # All retries exhausted — expire the subscription
            logger.error(f"Renewal exhausted for sub_id={subscription_id}. Expiring.")
            try:
                sub = VendorSubscription.objects.select_related('vendor').get(pk=subscription_id)
                sub.status = 'expired'
                sub.save(update_fields=['status'])

                services._sync_vendor_flags(sub.vendor)
                send_subscription_expired_email.delay(sub.vendor.id)
            except Exception as inner:
                logger.error(f"Failed to expire sub_id={subscription_id}: {inner}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Expire overdue subscriptions (safety net)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="subscriptions.expire_old_subscriptions")
def expire_old_subscriptions():
    """
    Marks active-but-past-their-end-date subscriptions as expired.
    Runs as a safety net at midnight.
    """
    from .models import VendorSubscription
    from . import services

    overdue = VendorSubscription.objects.filter(
        status='active',
        auto_renew=False,
        end_date__lt=timezone.now(),
    ).select_related('vendor')

    count = 0
    for sub in overdue:
        sub.status = 'expired'
        sub.save(update_fields=['status'])
        services._sync_vendor_flags(sub.vendor)
        count += 1

    logger.info(f"expire_old_subscriptions: expired {count} subscriptions")
    return f"expired:{count}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Warn vendors whose subscription expires in 3 days
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="subscriptions.warn_expiring_soon")
def warn_expiring_soon():
    """Sends warning emails to vendors whose subscription expires in 3 days."""
    from .models import VendorSubscription

    in_3_days = timezone.now() + timedelta(days=3)

    expiring = VendorSubscription.objects.filter(
        status='active',
        auto_renew=False,
        end_date__date=in_3_days.date(),
    ).select_related('vendor', 'plan')

    for sub in expiring:
        send_expiring_soon_email.delay(sub.vendor.id, sub.id)

    return f"warned:{expiring.count()}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Email tasks
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="subscriptions.send_subscription_confirmation_email")
def send_subscription_confirmation_email(vendor_id: int, subscription_id: int):
    from vendor.models import Vendor
    from .models import VendorSubscription
    from django.core.mail import send_mail
    from django.conf import settings

    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)

        send_mail(
            subject=f"Your {sub.plan.name} Plan is now active — Negromart",
            message=(
                f"Hi {vendor.name},\n\n"
                f"Your {sub.plan.name} subscription is now active.\n"
                f"Plan: {sub.plan.name}\n"
                f"Renews: {sub.end_date.strftime('%B %d, %Y')}\n\n"
                f"Manage your subscription at {settings.FRONTEND_URL}/subscription/\n\n"
                f"— The Negromart Team"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[vendor.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.error(f"send_subscription_confirmation_email failed: {e}")


@shared_task(name="subscriptions.send_renewal_success_email")
def send_renewal_success_email(vendor_id: int):
    from vendor.models import Vendor
    from .models import VendorSubscription
    from django.core.mail import send_mail
    from django.conf import settings

    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub = VendorSubscription.objects.filter(
            vendor=vendor, status='active'
        ).select_related('plan').first()

        if not sub:
            return

        send_mail(
            subject=f"Subscription renewed — Negromart",
            message=(
                f"Hi {vendor.name},\n\n"
                f"Your {sub.plan.name} plan has been renewed successfully.\n"
                f"GHS {sub.plan.price} has been charged to your saved card.\n"
                f"Next renewal: {sub.end_date.strftime('%B %d, %Y')}\n\n"
                f"— The Negromart Team"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[vendor.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.error(f"send_renewal_success_email failed: {e}")


@shared_task(name="subscriptions.send_payment_method_required_email")
def send_payment_method_required_email(vendor_id: int):
    from vendor.models import Vendor
    from django.core.mail import send_mail
    from django.conf import settings

    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        send_mail(
            subject="Action required: Update your payment method — Negromart",
            message=(
                f"Hi {vendor.name},\n\n"
                f"We couldn't renew your subscription because no valid payment method was found.\n\n"
                f"Update your payment method here: {settings.FRONTEND_URL}/vendor/subscription/\n\n"
                f"Your subscription has been paused. Update your card to restore access.\n\n"
                f"— The Negromart Team"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[vendor.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.error(f"send_payment_method_required_email failed: {e}")


@shared_task(name="subscriptions.send_cancellation_email")
def send_cancellation_email(vendor_id: int, subscription_id: int):
    from vendor.models import Vendor
    from .models import VendorSubscription
    from django.core.mail import send_mail
    from django.conf import settings

    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)

        send_mail(
            subject="Subscription cancelled — Negromart",
            message=(
                f"Hi {vendor.name},\n\n"
                f"Your {sub.plan.name} subscription has been cancelled.\n"
                f"You'll keep full access until {sub.end_date.strftime('%B %d, %Y')}.\n\n"
                f"Changed your mind? Resubscribe at {settings.FRONTEND_URL}/vendor/subscription/\n\n"
                f"— The Negromart Team"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[vendor.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.error(f"send_cancellation_email failed: {e}")


@shared_task(name="subscriptions.send_expiring_soon_email")
def send_expiring_soon_email(vendor_id: int, subscription_id: int):
    from vendor.models import Vendor
    from .models import VendorSubscription
    from django.core.mail import send_mail
    from django.conf import settings

    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)

        send_mail(
            subject=f"Your {sub.plan.name} plan expires in 3 days — Negromart",
            message=(
                f"Hi {vendor.name},\n\n"
                f"Your {sub.plan.name} subscription expires on {sub.end_date.strftime('%B %d, %Y')}.\n\n"
                f"Renew now to keep your store features: {settings.FRONTEND_URL}/vendor/subscription/\n\n"
                f"— The Negromart Team"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[vendor.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.error(f"send_expiring_soon_email failed: {e}")


@shared_task(name="subscriptions.send_subscription_expired_email")
def send_subscription_expired_email(vendor_id: int):
    from vendor.models import Vendor
    from django.core.mail import send_mail
    from django.conf import settings

    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        send_mail(
            subject="Your subscription has expired — Negromart",
            message=(
                f"Hi {vendor.name},\n\n"
                f"Your subscription has expired and your store has been moved to the Free plan.\n"
                f"Your products and data are safe.\n\n"
                f"Resubscribe to restore your features: {settings.FRONTEND_URL}/vendor/subscription/\n\n"
                f"— The Negromart Team"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[vendor.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.error(f"send_subscription_expired_email failed: {e}")


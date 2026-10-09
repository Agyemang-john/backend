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
from . import email_tasks  # noqa: F401 — registers subscriptions.* task names with Celery

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
    from django.db import transaction
    from order.pricing import order_unit_price, reserve_flash_units
    from order.stock import deduct_stock

    try:
        # Atomic so a retry after a failure part-way doesn't leave a half-built
        # order or count flash-sale units twice.
        with transaction.atomic():
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

                quantity = item_data["quantity"]
                if item_data.get("amount") is not None:
                    # Priced at payment time (VerifyPaymentAPIView) — what the customer paid
                    amount = Decimal(item_data["amount"])
                    reserved = reserve_flash_units(item_data.get("flash_sale_id"), item_data.get("flash_units") or 0)
                    if reserved < (item_data.get("flash_units") or 0):
                        logger.warning(
                            f"Flash sale {item_data.get('flash_sale_id')} sold out during payment {reference}; "
                            f"honoured {item_data.get('flash_units')} paid units, {reserved} were left."
                        )
                else:
                    # Tasks queued before this change carry no prices
                    amount = (variant.price if variant else product.price) * quantity

                order_products.append(OrderProduct(
                    order=order,
                    product=product,
                    variant=variant,
                    quantity=quantity,
                    price=order_unit_price(amount, quantity),
                    amount=amount,
                    selected_delivery_option_id=item_data["delivery_option_id"],
                ))

                # Update stock deduction
                deduct_stock(product, variant, quantity)

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
            from notification.utils import send_notification
            for vendor in unique_vendors:
                if hasattr(vendor, 'user') and vendor.user:
                    send_notification(
                        recipient=vendor.user,
                        verb="vendor_new_order",
                        actor=user,
                        target=order,
                        data={
                            "order_number": order.order_number,
                            "total_amount": f"GHS {order.total:,.2f}",
                            "items_count": len(order_products),
                            "buyer_name": user.first_name or user.email,
                            "message": f"New order #{order.order_number} — GHS {order.total:,.2f}",
                            "url": f"https://seller.negromart.com/orders/{order.id}/detail/",
                        }
                    )
                else:
                    logger.warning(f"Vendor {vendor.name} has no linked user. Notification skipped.")

            # Notify buyer: order confirmed
            send_notification(
                recipient=user,
                verb="customer_order_placed",
                target=order,
                data={
                    "order_number": order.order_number,
                    "total_amount": f"GHS {order.total:,.2f}",
                    "message": f"Your order #{order.order_number} has been placed successfully!",
                    "url": f"https://www.negromart.com/dashboard/order-history/{order.id}/",
                }
            )

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


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE charge_vendor_for_renewal() in subscriptions/tasks.py with this.
# Reads max_retries from SubscriptionEmailConfig instead of hardcoding 3.
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(
    name='subscriptions.charge_vendor_for_renewal',
    bind=True,
    max_retries=10,          # ceiling — actual limit comes from DB config
    default_retry_delay=86400,  # 24 hours between retries
)
def charge_vendor_for_renewal(self, subscription_id: int):
    """
    Charges a single vendor's saved card or MoMo for renewal.
    Retries up to renewal_max_retries times (from SubscriptionEmailConfig),
    once per day, before expiring the subscription.
    """
    from . import services
    from .models import VendorSubscription
    from .email_models import SubscriptionEmailConfig

    cfg         = SubscriptionEmailConfig.get()
    max_retries = cfg.renewal_max_retries  # reads live from DB

    try:
        result = services.charge_for_renewal(subscription_id)

        if result.get('status') == 'pending_momo':
            # MoMo was initiated — the vendor needs to approve on their phone.
            # Poll will activate the subscription when they do.
            # Log and return — don't retry (retrying would fire another USSD prompt).
            logger.info(
                f'MoMo renewal initiated for sub={subscription_id}: '
                f'ref={result.get("reference")} — waiting for vendor approval'
            )
            return result

        logger.info(f'Renewal success: sub={subscription_id} ref={result.get("reference")}')
        return result

    except Exception as exc:
        attempt = self.request.retries + 1
        logger.warning(
            f'Renewal attempt {attempt}/{max_retries} failed '
            f'for sub={subscription_id}: {exc}'
        )

        # On the very first failure, immediately alert the vendor so they can
        # update their payment method before the next automatic retry tomorrow.
        if self.request.retries == 0:
            try:
                _sub = VendorSubscription.objects.select_related('vendor').get(pk=subscription_id)
                from .email_tasks import send_payment_method_required_email
                send_payment_method_required_email.delay(_sub.vendor.id)
            except Exception as notify_exc:
                logger.error(
                    f'Failed to send first-failure notification for sub={subscription_id}: {notify_exc}'
                )

        if self.request.retries < max_retries - 1:
            raise self.retry(exc=exc)

        # All retries exhausted — mark expired and notify the vendor
        logger.error(f'Renewal exhausted ({max_retries} attempts) for sub={subscription_id}. Expiring.')
        try:
            sub = VendorSubscription.objects.select_related('vendor').get(pk=subscription_id)
            sub.status = 'expired'
            sub.save(update_fields=['status'])
            services._sync_vendor_flags(sub.vendor)

            from .email_tasks import send_subscription_expired_email
            send_subscription_expired_email.delay(sub.vendor.id)
        except Exception as inner:
            logger.error(f'Failed to expire sub={subscription_id}: {inner}')

# ─────────────────────────────────────────────────────────────────────────────
# Seller ledger & payouts (payments/ledger.py)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(ignore_result=True)
def sweep_delivered_shipments():
    """
    Safety net for seller earnings: post any delivered shipment the ledger
    hasn't seen. Deliveries marked from Django admin bulk actions (which use
    queryset.update and skip signals) or lost on_commit hooks get picked up
    here within one run. Idempotent; runs every 15 minutes.
    """
    from order.fulfilment import on_shipment_delivered
    from order.models import Shipment
    from .ledger import post_shipment_earnings
    pending = Shipment.objects.filter(status='delivered', ledger_posted_at__isnull=True)
    posted = 0
    for shipment in pending.iterator(chunk_size=500):
        try:
            # Stamps delivered dates on the lines (admin bulk updates skip
            # that too), then post synchronously so the count is accurate.
            on_shipment_delivered(shipment)
            posted += bool(post_shipment_earnings(shipment.pk))
        except Exception as exc:
            logger.error(f"ledger sweep failed for shipment={shipment.pk}: {exc}")
    if posted:
        logger.info(f"ledger sweep posted {posted} shipment(s)")
    return posted


@shared_task(ignore_result=True)
def run_seller_payouts():
    """
    Pay each store with a verified payout method its available balance.
    Disabled unless settings.SELLER_PAYOUTS_ENABLED is True, so turning on
    real transfers is an explicit decision per environment.
    """
    from django.conf import settings as dj_settings
    from vendor.models import Vendor
    from .ledger import pay_vendor

    if not getattr(dj_settings, 'SELLER_PAYOUTS_ENABLED', False):
        logger.info("seller payouts disabled (SELLER_PAYOUTS_ENABLED is False)")
        return 0
    paid = 0
    vendors = Vendor.objects.filter(payment_methods__status='verified', is_suspended=False).distinct()
    for vendor in vendors.iterator():
        try:
            payout = pay_vendor(vendor)
            paid += bool(payout and payout.status == 'success')
        except Exception as exc:
            logger.error(f"payout failed for vendor={vendor.pk}: {exc}")
    return paid

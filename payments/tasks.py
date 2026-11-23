from celery import shared_task
from order.sourcing import snapshot as sourcing_snapshot
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
    reference,
    coupon=None,
    coupons=None,
    code_id=None,
):
    from django.db import transaction
    from order.coupons import redeem
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
                    # Coupon share, fixed at payment time (order/coupons.py)
                    discount=Decimal(item_data.get("discount") or 0),
                    seller_discount=Decimal(item_data.get("seller_discount") or 0),
                    selected_delivery_option_id=item_data["delivery_option_id"],
                    **sourcing_snapshot(product, variant, delivery_option_id=item_data["delivery_option_id"]),
                ))

                # Update stock deduction
                deduct_stock(product, variant, quantity)

            # Bulk create order products
            OrderProduct.objects.bulk_create(order_products)
            order.vendors.set(unique_vendors)
            if coupon and not coupons:      # task queued before clip coupons existed
                coupons, code_id = [coupon], coupon.get("id")
            redeem(order, coupons, code_id=code_id)

            # Generate unique order number
            while True:
                order_number = f"INVOICE_NO-{get_random_string(8).upper()}"
                if not Order.objects.filter(order_number=order_number).exists():
                    break

            order.order_number = order_number
            order.save()

            # Stores' own systems (seller API webhooks, tools/webhooks.py).
            from tools.webhooks import order_event_later
            order_event_later(order.pk, 'order.created')

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
# Subscription renewal (payments/subscription_lifecycle.py)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='subscriptions.charge_vendor_for_renewal', ignore_result=True)
def charge_vendor_for_renewal(subscription_id: int):
    """
    One renewal attempt for one subscription. No Celery retries: the next
    attempt time is stored on the subscription and picked up by the hourly
    process_renewals run. (Long retry countdowns on a Redis broker get
    re-delivered after the visibility timeout, which charged cards twice.)
    The attempt claims the row under a lock, so a duplicate task is a no-op.
    """
    from .subscription_lifecycle import attempt_renewal
    try:
        result = attempt_renewal(subscription_id)
    except Exception as exc:
        logger.error(f'Renewal attempt crashed for sub={subscription_id}: {exc}', exc_info=True)
        return {'status': 'error'}
    logger.info(f'Renewal attempt sub={subscription_id}: {result.get("status")} ref={result.get("reference")}')
    return result


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

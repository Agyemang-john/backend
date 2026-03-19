# subscriptions/services.py
"""
Service layer — all business logic lives here, not in views or models.
Views are thin. Models are thin. This file is thick.
"""

import uuid
import hashlib
import hmac
import logging
import requests
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.db import transaction

from .models import (
    SubscriptionPlan,
    VendorSubscription,
    SubscriptionUsage,
    PaystackCustomer,
    PaystackAuthorization,
    PaymentTransaction,
)

logger = logging.getLogger(__name__)

PAYSTACK_BASE = "https://api.paystack.co"
PAYSTACK_HEADERS = {
    "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
    "Content-Type": "application/json",
}

BILLING_DAYS = {
    "monthly": 30,
    "quarterly": 90,
    "yearly": 365,
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Plan helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_active_plans():
    """Return all active plans ordered by price for the plan listing page."""
    return SubscriptionPlan.objects.filter(is_active=True).order_by('price')


def get_plan_for_billing(plan_id: int, billing: str) -> SubscriptionPlan:
    """
    Fetch the correct plan variant based on billing cycle.
    For yearly billing, we look for a plan with the same tier but billing_cycle='yearly'.
    Falls back to the same plan if no yearly variant exists.
    """
    plan = SubscriptionPlan.objects.get(pk=plan_id, is_active=True)

    if billing == "yearly" and plan.billing_cycle == "monthly":
        # Try to find the yearly sibling (same tier, billing_cycle='yearly')
        yearly = SubscriptionPlan.objects.filter(
            tier=plan.tier,
            billing_cycle='yearly',
            is_active=True
        ).first()
        if yearly:
            return yearly

    return plan


# ─────────────────────────────────────────────────────────────────────────────
# 2. Initiate subscription — returns Paystack authorization_url
# ─────────────────────────────────────────────────────────────────────────────

# Tier ordering used to determine upgrade vs downgrade direction
TIER_ORDER = {"free": 0, "basic": 1, "pro": 2, "enterprise": 3}


def initiate_subscription(vendor, plan_id: int, billing: str) -> dict:
    """
    Called when vendor clicks "Subscribe now".
    1. Resolves the correct plan
    2. Detects whether this is a new subscription, upgrade, or downgrade
    3. Creates a pending PaymentTransaction with the correct type
    4. Calls Paystack /transaction/initialize
    5. Returns the authorization_url + upgrade_info for the frontend confirmation message
    """
    plan = get_plan_for_billing(plan_id, billing)

    # Free plan — activate immediately, no payment needed
    if plan.price == 0:
        return _activate_free_plan(vendor, plan)

    # ── Detect subscription change type ──────────────────────────────────
    existing_sub = VendorSubscription.objects.filter(
        vendor=vendor, status='active'
    ).select_related('plan').first()

    if existing_sub and existing_sub.plan.tier != 'free':
        old_tier_rank = TIER_ORDER.get(existing_sub.plan.tier, 0)
        new_tier_rank = TIER_ORDER.get(plan.tier, 0)
        if new_tier_rank > old_tier_rank:
            txn_type = 'upgrade'
        elif new_tier_rank < old_tier_rank:
            txn_type = 'downgrade'
        else:
            # Same tier, different billing cycle (e.g. monthly → yearly)
            txn_type = 'upgrade'
    else:
        txn_type = 'initial'

    reference = _generate_reference(vendor, "SUB")

    # Create pending transaction record before going to Paystack
    # This ensures we have an audit trail even if the vendor closes the window
    transaction_obj = PaymentTransaction.objects.create(
        vendor=vendor,
        transaction_type=txn_type,
        amount=plan.price,
        currency='GHS',
        status='pending',
        paystack_reference=reference,
    )

    payload = {
        "email": vendor.email,
        "amount": int(plan.price * 100),   # Paystack uses pesewas
        "reference": reference,
        "callback_url": f"{settings.SITE_URL}/subscription/verify/?ref={reference}",
        "metadata": {
            "vendor_id": str(vendor.id),
            "plan_id": str(plan.id),
            "billing": billing,
            "transaction_db_id": str(transaction_obj.id),
            "transaction_type": txn_type,
        },
        "channels": ["card", "mobile_money", "bank_transfer"],
    }

    try:
        response = requests.post(
            f"{PAYSTACK_BASE}/transaction/initialize",
            json=payload,
            headers=PAYSTACK_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("status"):
            raise Exception(f"Paystack error: {data.get('message')}")

        result = {
            "authorization_url": data["data"]["authorization_url"],
            "reference": reference,
            "access_code": data["data"]["access_code"],
            "transaction_type": txn_type,
        }

        # Include context for the frontend confirmation message on upgrades/downgrades
        if txn_type in ('upgrade', 'downgrade') and existing_sub:
            result["upgrade_info"] = {
                "from_plan": existing_sub.plan.name,
                "to_plan": plan.name,
                "charged_today": str(plan.price),
                "current_plan_ends_now": True,
                "new_plan_starts_today": True,
            }

        return result

    except Exception as e:
        # Mark the pending transaction as failed
        transaction_obj.status = 'failed'
        transaction_obj.failure_reason = str(e)
        transaction_obj.save()
        logger.error(f"Paystack initiate failed for vendor {vendor.id}: {e}")
        raise


def _activate_free_plan(vendor, plan: SubscriptionPlan) -> dict:
    """Activates the free plan directly — no Paystack involved."""
    with transaction.atomic():
        # Cancel any existing active subscription
        VendorSubscription.objects.filter(
            vendor=vendor, status='active'
        ).update(status='cancelled')

        sub = VendorSubscription.objects.create(
            vendor=vendor,
            plan=plan,
            status='active',
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(days=36500),  # 100 years = forever
            auto_renew=False,
        )
        _ensure_usage_record(vendor, sub)
        _sync_vendor_flags(vendor)

    return {"activated": True, "plan": plan.name}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Verify payment — called after Paystack redirects back
# ─────────────────────────────────────────────────────────────────────────────

def verify_and_activate(reference: str) -> dict:
    """
    Called from the /verify/ endpoint after Paystack redirects.
    1. Calls Paystack /transaction/verify/{reference}
    2. Saves the authorization (card token)
    3. Activates the VendorSubscription
    4. Updates the transaction record
    """
    try:
        response = requests.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=PAYSTACK_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        ps_data = response.json()["data"]
    except Exception as e:
        logger.error(f"Paystack verify failed for ref {reference}: {e}")
        raise

    if ps_data["status"] != "success":
        # Update transaction record
        PaymentTransaction.objects.filter(paystack_reference=reference).update(
            status='failed',
            failure_reason=ps_data.get("gateway_response", "Payment not successful"),
        )
        raise Exception("Payment was not successful.")

    meta = ps_data.get("metadata", {})
    from vendor.models import Vendor
    vendor = Vendor.objects.get(pk=meta["vendor_id"])
    plan = SubscriptionPlan.objects.get(pk=meta["plan_id"])
    billing = meta.get("billing", "monthly")
    txn_type = meta.get("transaction_type", "initial")

    with transaction.atomic():
        # 1. Save/update Paystack customer
        customer, _ = PaystackCustomer.objects.get_or_create(
            vendor=vendor,
            defaults={
                "customer_code": ps_data["customer"]["customer_code"],
                "email": vendor.email,
            },
        )

        # 2. Save the authorization code (enables future auto-billing)
        auth_data = ps_data.get("authorization", {})
        if auth_data.get("reusable"):
            authorization, created = PaystackAuthorization.objects.get_or_create(
                authorization_code=auth_data["authorization_code"],
                defaults={
                    "vendor": vendor,
                    "paystack_customer": customer,
                    "card_type": auth_data.get("card_type", ""),
                    "last4": auth_data.get("last4", ""),
                    "exp_month": auth_data.get("exp_month", ""),
                    "exp_year": auth_data.get("exp_year", ""),
                    "bank": auth_data.get("bank", ""),
                    "is_reusable": True,
                    "is_default": True,
                },
            )
            if created:
                # Make only this card the default, unset others
                PaystackAuthorization.objects.filter(
                    vendor=vendor
                ).exclude(pk=authorization.pk).update(is_default=False)

        # 3. Cancel existing active subscriptions
        VendorSubscription.objects.filter(
            vendor=vendor, status='active'
        ).update(status='cancelled')

        # 4. Create the new active subscription
        days = BILLING_DAYS.get(billing, 30)
        sub = VendorSubscription.objects.create(
            vendor=vendor,
            plan=plan,
            status='active',
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(days=days),
            auto_renew=True,
            payment_reference=reference,
        )

        # 5. Update/create usage tracker
        _ensure_usage_record(vendor, sub)

        # 6. Update the transaction record
        PaymentTransaction.objects.filter(paystack_reference=reference).update(
            subscription=sub,
            status='success',
            paystack_transaction_id=str(ps_data.get("id", "")),
            paid_at=timezone.now(),
        )

        # 7. Sync vendor model flags
        _sync_vendor_flags(vendor)

    # Fire confirmation email OUTSIDE the transaction so the task worker
    # always reads fully-committed subscription data.
    from .tasks import send_subscription_confirmation_email
    send_subscription_confirmation_email.delay(vendor.id, sub.id)

    logger.info(f"Subscription {txn_type}: vendor={vendor.id}, plan={plan.name}")
    return {
        "vendor_id": vendor.id,
        "plan": plan.name,
        "status": "active",
        "end_date": sub.end_date.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Auto-renewal (called by Celery Beat)
# ─────────────────────────────────────────────────────────────────────────────

def charge_for_renewal(subscription_id: int) -> dict:
    """
    Silently charges the vendor's saved card.
    Called by the Celery task `charge_vendor_for_renewal`.
    """
    sub = VendorSubscription.objects.select_related(
        'vendor', 'plan'
    ).get(pk=subscription_id)
    vendor = sub.vendor
    plan = sub.plan

    authorization = PaystackAuthorization.objects.filter(
        vendor=vendor, is_default=True, is_reusable=True
    ).first()

    if not authorization:
        sub.status = 'past_due'
        sub.save()
        from .tasks import send_payment_method_required_email
        send_payment_method_required_email.delay(vendor.id)
        return {"status": "past_due", "reason": "No saved card found"}

    reference = _generate_reference(vendor, "RNW")

    # Create pending transaction before charging
    txn = PaymentTransaction.objects.create(
        vendor=vendor,
        subscription=sub,
        authorization=authorization,
        transaction_type='renewal',
        amount=plan.price,
        currency='GHS',
        status='pending',
        paystack_reference=reference,
    )

    try:
        response = requests.post(
            f"{PAYSTACK_BASE}/transaction/charge_authorization",
            json={
                "authorization_code": authorization.authorization_code,
                "email": vendor.email,
                "amount": int(plan.price * 100),
                "reference": reference,
                "metadata": {
                    "payment_type": "renewal",
                    "vendor_id": str(vendor.id),
                    "subscription_id": str(sub.id),
                },
            },
            headers=PAYSTACK_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        ps_data = response.json()["data"]

        if ps_data["status"] == "success":
            with transaction.atomic():
                sub.renew()

                txn.status = 'success'
                txn.paystack_transaction_id = str(ps_data.get("id", ""))
                txn.paid_at = timezone.now()
                txn.save()

                _sync_vendor_flags(vendor)

            # Email dispatched after commit — task worker reads committed data
            from .tasks import send_renewal_success_email
            send_renewal_success_email.delay(vendor.id)

            return {"status": "success", "reference": reference}
        else:
            raise Exception(ps_data.get("gateway_response", "Charge failed"))

    except Exception as e:
        txn.status = 'failed'
        txn.failure_reason = str(e)
        txn.save()
        logger.warning(f"Renewal charge failed for vendor {vendor.id}: {e}")
        raise


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cancellation
# ─────────────────────────────────────────────────────────────────────────────

def cancel_subscription(vendor, reason: str = "") -> VendorSubscription:
    """
    Cancels the vendor's active subscription.
    Access is retained until end_date (like Stripe / Digital Ocean).

    select_for_update() requires an open transaction — the entire function
    is wrapped in transaction.atomic() so the row lock is valid.
    """
    with transaction.atomic():
        sub = VendorSubscription.objects.filter(
            vendor=vendor, status='active'
        ).select_for_update().first()

        if not sub:
            raise Exception("No active subscription found.")

        sub.cancel(reason=reason)

    # Fire the email outside the transaction — no need to hold the lock
    # while Celery enqueues the task.
    from .tasks import send_cancellation_email
    send_cancellation_email.delay(vendor.id, sub.id)

    logger.info(f"Subscription cancelled: vendor={vendor.id}")
    return sub


def initiate_card_add(vendor) -> dict:
    """
    Initiates a GHS 1.00 Paystack charge to tokenize a new card.
 
    Flow:
    1. Charge GHS 1.00 via /transaction/initialize with channel='card' only
    2. Paystack returns an authorization_url — vendor completes the charge
    3. Paystack redirects to callback_url with reference
    4. Frontend calls GET /billing/verify-card/?ref=CARD-ADD-xxx
    5. verify_card_add() saves the reusable authorization and refunds the GHS 1.00
 
    Returns:
        { authorization_url, reference }
    """
    reference = _generate_reference(vendor, "CARD-ADD")
 
    payload = {
        "email":    vendor.email,
        "amount":   100,          # GHS 1.00 in pesewas — minimum Paystack accepts
        "currency": "GHS",
        "reference": reference,
        "callback_url": f"{settings.SITE_URL}/vendor/billing/cards?card_ref={reference}",
        "channels": ["card"],     # Card only — not MoMo or bank transfer
        "metadata": {
            "purpose":   "card_add",
            "vendor_id": str(vendor.id),
        },
    }
 
    try:
        resp = requests.post(
            f"{PAYSTACK_BASE}/transaction/initialize",
            json=payload,
            headers=PAYSTACK_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
 
        if not data.get("status"):
            raise Exception(f"Paystack error: {data.get('message', 'Unknown error')}")
 
        logger.info(f"Card add initiated: vendor={vendor.id} ref={reference}")
        return {
            "authorization_url": data["data"]["authorization_url"],
            "reference":         reference,
            "access_code":       data["data"]["access_code"],
        }
 
    except Exception as exc:
        logger.error(f"initiate_card_add failed for vendor {vendor.id}: {exc}")
        raise
 
 
def verify_card_add(reference: str) -> dict:
    """
    Called after Paystack redirects back from the card tokenization charge.
 
    1. Verifies the GHS 1.00 charge succeeded at Paystack
    2. Saves the reusable authorization (card token) to PaystackAuthorization
    3. Refunds the GHS 1.00 so the vendor isn't actually charged
    4. Returns { card_saved: bool, last4, card_type, expiry_display }
 
    If a card with this authorization_code already exists (duplicate), returns
    { card_saved: False, reason: 'duplicate' } without raising.
    """
    try:
        resp = requests.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=PAYSTACK_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        ps_data = resp.json().get("data", {})
    except Exception as exc:
        logger.error(f"verify_card_add: Paystack verify failed ref={reference}: {exc}")
        raise
 
    if ps_data.get("status") != "success":
        raise Exception("Card charge was not successful — card not saved.")
 
    meta = ps_data.get("metadata") or {}
    if meta.get("purpose") != "card_add":
        # This reference is for a subscription, not a card add — don't process here
        raise Exception("Reference is not a card-add transaction.")
 
    from vendor.models import Vendor
    vendor = Vendor.objects.get(pk=meta["vendor_id"])
 
    auth_data = ps_data.get("authorization", {})
    auth_code = auth_data.get("authorization_code", "")
 
    if not auth_code or not auth_data.get("reusable"):
        raise Exception("Card is not reusable — it cannot be saved for future charges.")
 
    # Prevent duplicates
    if PaystackAuthorization.objects.filter(authorization_code=auth_code).exists():
        logger.info(f"verify_card_add: duplicate card {auth_code[-6:]} for vendor {vendor.id}")
        # Refund the GHS 1.00 even for duplicates
        _refund_card_add_charge(ps_data.get("id"), vendor.id)
        return {"card_saved": False, "reason": "duplicate", "message": "This card is already saved."}
 
    # Ensure PaystackCustomer record exists
    customer, _ = PaystackCustomer.objects.get_or_create(
        vendor=vendor,
        defaults={
            "customer_code": ps_data["customer"]["customer_code"],
            "email":         vendor.email,
        },
    )
 
    # First card added becomes the default
    is_first = not PaystackAuthorization.objects.filter(vendor=vendor, is_reusable=True).exists()
 
    authorization = PaystackAuthorization.objects.create(
        vendor              = vendor,
        paystack_customer   = customer,
        authorization_code  = auth_code,
        card_type           = auth_data.get("card_type", ""),
        last4               = auth_data.get("last4", ""),
        exp_month           = auth_data.get("exp_month", ""),
        exp_year            = auth_data.get("exp_year", ""),
        bank                = auth_data.get("bank", ""),
        is_reusable         = True,
        is_default          = is_first,
    )
 
    if is_first:
        # Ensure no other cards are incorrectly marked default
        PaystackAuthorization.objects.filter(
            vendor=vendor
        ).exclude(pk=authorization.pk).update(is_default=False)
 
    logger.info(
        f"Card saved: vendor={vendor.id} last4={auth_data.get('last4')} "
        f"type={auth_data.get('card_type')} default={is_first}"
    )
 
    # Refund the GHS 1.00 charge — fire and forget (don't fail if refund fails)
    _refund_card_add_charge(ps_data.get("id"), vendor.id)
 
    exp_month = auth_data.get("exp_month", "")
    exp_year  = auth_data.get("exp_year", "")
    expiry    = f"{exp_month}/{exp_year[2:]}" if exp_month and exp_year else ""
 
    return {
        "card_saved":    True,
        "last4":         auth_data.get("last4", ""),
        "card_type":     auth_data.get("card_type", ""),
        "expiry_display": expiry,
        "bank":          auth_data.get("bank", ""),
        "is_default":    is_first,
        "message":       "Card saved successfully.",
    }
 
 
def _refund_card_add_charge(transaction_id, vendor_id):
    """
    Refunds the GHS 1.00 card-add charge.
    Called after saving the authorization.
    Failures are logged but do NOT raise — the card is already saved.
    """
    if not transaction_id:
        return
 
    try:
        resp = requests.post(
            f"{PAYSTACK_BASE}/refund",
            json={
                "transaction": transaction_id,
                "amount":      100,   # GHS 1.00 in pesewas
            },
            headers=PAYSTACK_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("status"):
            logger.info(f"GHS 1.00 card-add charge refunded: txn={transaction_id} vendor={vendor_id}")
        else:
            logger.warning(f"Refund failed for txn={transaction_id}: {data.get('message')}")
    except Exception as exc:
        logger.warning(f"Refund request failed for txn={transaction_id}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Webhook handler
# ─────────────────────────────────────────────────────────────────────────────

def handle_paystack_webhook(payload: dict, signature: str, raw_body: bytes) -> str:
    """
    Verifies Paystack webhook signature and routes the event.
    Returns a string describing what was done (for logging).
    """
    _verify_webhook_signature(raw_body, signature)

    event = payload["event"]
    data = payload["data"]

    handlers = {
        "charge.success": _webhook_charge_success,
        "charge.failed": _webhook_charge_failed,
        "invoice.payment_failed": _webhook_invoice_failed,
        "refund.processed": _webhook_refund_processed,
    }

    handler = handlers.get(event)
    if handler:
        return handler(data)

    logger.info(f"Unhandled webhook event: {event}")
    return f"unhandled:{event}"


def _verify_webhook_signature(raw_body: bytes, signature: str):
    """HMAC-SHA512 signature verification — Paystack requirement."""
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode('utf-8'),
        raw_body,
        hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise PermissionError("Invalid webhook signature.")


def _webhook_charge_success(data: dict) -> str:
    reference = data.get("reference")
    txn = PaymentTransaction.objects.filter(paystack_reference=reference).first()
    if not txn:
        return f"charge.success: unknown reference {reference}"

    if txn.status != 'success':
        txn.status = 'success'
        txn.paystack_transaction_id = str(data.get("id", ""))
        txn.paid_at = timezone.now()
        txn.save()

    return f"charge.success: processed {reference}"


def _webhook_charge_failed(data: dict) -> str:
    reference = data.get("reference")
    PaymentTransaction.objects.filter(paystack_reference=reference).update(
        status='failed',
        failure_reason=data.get("gateway_response", "Charge failed"),
    )
    return f"charge.failed: {reference}"


def _webhook_invoice_failed(data: dict) -> str:
    """Triggered when a recurring charge fails on Paystack's side."""
    reference = data.get("reference", "")
    PaymentTransaction.objects.filter(paystack_reference=reference).update(
        status='failed',
        failure_reason="Invoice payment failed",
    )
    return f"invoice.payment_failed: {reference}"


def _webhook_refund_processed(data: dict) -> str:
    reference = data.get("transaction_reference", "")
    PaymentTransaction.objects.filter(paystack_reference=reference).update(
        status='refunded',
    )
    return f"refund.processed: {reference}"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _generate_reference(vendor, prefix: str) -> str:
    uid = uuid.uuid4().hex[:10].upper()
    return f"{prefix}-{vendor.id}-{uid}"


def _ensure_usage_record(vendor, subscription: VendorSubscription):
    """Create or reset the SubscriptionUsage record for a vendor."""
    usage, created = SubscriptionUsage.objects.get_or_create(
        vendor=vendor,
        defaults={
            "subscription": subscription,
            "active_products_count": 0,
            "period_start": timezone.now(),
        },
    )
    if not created:
        usage.subscription = subscription
        usage.reset_for_new_cycle()
    return usage


def _sync_vendor_flags(vendor):
    """
    Keeps the denormalized is_subscribed, subscription_start_date,
    subscription_end_date fields on the Vendor model in sync.
    """
    active_sub = VendorSubscription.objects.filter(
        vendor=vendor, status='active'
    ).order_by('-created_at').first()

    if active_sub and active_sub.end_date >= timezone.now():
        vendor.is_subscribed = True
        vendor.subscription_start_date = active_sub.start_date.date()
        vendor.subscription_end_date = active_sub.end_date.date()
    else:
        vendor.is_subscribed = False
        vendor.subscription_start_date = None
        vendor.subscription_end_date = None

    vendor.save(update_fields=[
        'is_subscribed', 'subscription_start_date', 'subscription_end_date'
    ])
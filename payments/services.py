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
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.db import models, transaction

from .models import (
    SubscriptionPlan,
    VendorSubscription,
    SubscriptionUsage,
    PaystackCustomer,
    PaystackAuthorization,
    PaymentTransaction,
)
from vendor.models import Vendor

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

# Proration below this is waived: the switch happens without a payment.
MIN_CHARGE = Decimal("1.00")


class SubscriptionChangeError(ValueError):
    """The requested plan change isn't allowed (same plan, nothing to cancel…)."""


def current_paid_subscription(vendor):
    """The store's active, unexpired, paid subscription, or None."""
    sub = (
        VendorSubscription.objects.select_related('plan', 'scheduled_plan')
        .filter(vendor=vendor, status='active', end_date__gt=timezone.now())
        .order_by('-created_at')
        .first()
    )
    return sub if sub and sub.plan.price > 0 else None


def quote_change(vendor, plan: SubscriptionPlan) -> dict:
    """
    What moving to `plan` costs today and when it takes effect.

    change_type:
      'initial'   no paid plan yet: full price, new period starts today
      'upgrade'   higher tier, or monthly → yearly: switch now and pay the
                  prorated difference (unused days on the old plan are credited)
      'downgrade' lower tier, or yearly → monthly: nothing to pay now; the
                  switch happens at the next renewal
      'current'   already on this plan

    Same billing cycle upgrades keep the renewal date. A cycle change starts
    a new period today; credit beyond the new price extends that period.
    """
    now = timezone.now()
    new_days = BILLING_DAYS.get(plan.billing_cycle, 30)
    sub = current_paid_subscription(vendor)

    base = {
        "plan_id": plan.id,
        "plan_name": plan.name,
        "billing_cycle": plan.billing_cycle,
        "price": plan.price,
        "credit": Decimal("0.00"),
        "current_plan": sub.plan.name if sub else None,
        "current_billing_cycle": sub.plan.billing_cycle if sub else None,
    }

    if sub is None:
        return {**base, "change_type": "initial", "amount_due": plan.price,
                "effective_date": now, "period_end": now + timedelta(days=new_days)}

    old = sub.plan
    if old.id == plan.id:
        return {**base, "change_type": "current", "amount_due": Decimal("0.00"),
                "effective_date": now, "period_end": sub.end_date}

    old_rank, new_rank = TIER_ORDER.get(old.tier, 0), TIER_ORDER.get(plan.tier, 0)
    old_days = BILLING_DAYS.get(old.billing_cycle, 30)
    is_upgrade = new_rank > old_rank or (new_rank == old_rank and new_days > old_days)

    if not is_upgrade:
        return {**base, "change_type": "downgrade", "amount_due": Decimal("0.00"),
                "effective_date": sub.end_date,
                "period_end": sub.end_date + timedelta(days=new_days)}

    remaining = max((sub.end_date - now).total_seconds(), 0)
    fraction = Decimal(str(min(remaining / (old_days * 86400), 1)))
    credit = (old.price * fraction).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if old.billing_cycle == plan.billing_cycle:
        amount = ((plan.price - old.price) * fraction).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        period_end = sub.end_date
    else:
        amount = plan.price - credit
        period_end = now + timedelta(days=new_days)
        if amount < 0:
            # Leftover credit buys extra days on the new plan.
            extra_days = float(-amount / plan.price) * new_days
            period_end += timedelta(days=extra_days)
            amount = Decimal("0.00")

    if amount < MIN_CHARGE:
        amount = Decimal("0.00")

    return {**base, "change_type": "upgrade", "credit": credit, "amount_due": amount,
            "effective_date": now, "period_end": period_end}


def schedule_downgrade(vendor, plan: SubscriptionPlan) -> VendorSubscription:
    """Queue `plan` for the next renewal. Re-enables renewal if it was off."""
    current = current_paid_subscription(vendor)
    if current is None:
        raise SubscriptionChangeError("You don't have a paid plan to change.")
    with transaction.atomic():
        sub = (VendorSubscription.objects.select_for_update()
               .filter(pk=current.pk, status='active').first())
        if sub is None:
            raise SubscriptionChangeError("Your plan changed in the meantime. Please refresh and try again.")
        sub.scheduled_plan = plan
        sub.auto_renew = True
        sub.cancelled_at = None
        sub.save(update_fields=['scheduled_plan', 'auto_renew', 'cancelled_at', 'updated_at'])
    return sub


def clear_scheduled_change(vendor) -> VendorSubscription:
    sub = current_paid_subscription(vendor)
    if not sub or not sub.scheduled_plan_id:
        raise SubscriptionChangeError("There is no scheduled plan change.")
    sub.scheduled_plan = None
    sub.save(update_fields=['scheduled_plan', 'updated_at'])
    return sub


def resume_subscription(vendor) -> VendorSubscription:
    sub = current_paid_subscription(vendor)
    if not sub or not sub.cancel_at_period_end:
        raise SubscriptionChangeError("There is no cancelled subscription to resume.")
    sub.resume()
    return sub


def _switch_plan_without_payment(vendor, plan, quote) -> dict:
    """An upgrade fully covered by credit: switch now and log a zero-amount record."""
    with transaction.atomic():
        Vendor.objects.select_for_update().get(pk=vendor.pk)
        # Re-quote under the lock: the subscription may have changed since.
        quote = quote_change(vendor, plan)
        sub = current_paid_subscription(vendor)
        if sub is None or quote["change_type"] != "upgrade" or quote["amount_due"] != 0:
            raise SubscriptionChangeError("Prices changed in the meantime. Please review the new amount.")
        sub = VendorSubscription.objects.select_for_update().get(pk=sub.pk)
        sub.plan = plan
        sub.end_date = quote["period_end"]
        sub.scheduled_plan = None
        sub.auto_renew = True
        sub.cancelled_at = None
        sub.save()
        PaymentTransaction.objects.create(
            vendor=vendor, subscription=sub, transaction_type='upgrade',
            amount=Decimal("0.00"), currency='GHS', status='success',
            paystack_reference=_generate_reference(vendor, "UPG"), paid_at=timezone.now(),
            plan=plan, billing=plan.billing_cycle,
        )
        from .subscription_lifecycle import refresh_store
        refresh_store(vendor)
    return {"activated": True, "plan": plan.name, "end_date": sub.end_date.isoformat()}


def prepare_plan_change(vendor, plan_id: int, billing: str):
    """
    Shared front half of every checkout (Paystack, MoMo, saved card).
    Returns (plan, quote, early_result). When early_result is set the change
    needed no payment and has already been applied.
    """
    plan = get_plan_for_billing(plan_id, billing)

    if plan.price == 0:
        if current_paid_subscription(vendor):
            raise SubscriptionChangeError(
                "To move to the Free plan, cancel your subscription. "
                "You keep your current plan until the end of the period."
            )
        return plan, None, _activate_free_plan(vendor, plan)

    quote = quote_change(vendor, plan)
    if quote["change_type"] == "current":
        raise SubscriptionChangeError("You're already on this plan.")
    if quote["change_type"] == "downgrade":
        sub = schedule_downgrade(vendor, plan)
        return plan, quote, {
            "scheduled": True, "plan": plan.name,
            "effective_date": sub.end_date.isoformat(),
        }
    if quote["amount_due"] == 0 and quote["change_type"] == "upgrade":
        return plan, quote, _switch_plan_without_payment(vendor, plan, quote)
    return plan, quote, None


def payment_metadata(vendor, plan, billing, quote, **extra) -> dict:
    """Metadata carried through Paystack so activation applies the quoted period."""
    return {
        "vendor_id": str(vendor.id),
        "plan_id": str(plan.id),
        "billing": billing,
        "transaction_type": quote["change_type"],
        "period_end": quote["period_end"].isoformat(),
        **extra,
    }


def initiate_subscription(vendor, plan_id: int, billing: str) -> dict:
    """
    Called when vendor clicks "Subscribe now".
    Downgrades are scheduled and fully-credited upgrades applied straight
    away (no Paystack). Everything else gets a Paystack checkout for the
    quoted amount.
    """
    plan, quote, early = prepare_plan_change(vendor, plan_id, billing)
    if early:
        return early

    txn_type = quote["change_type"]
    reference = _generate_reference(vendor, "SUB")

    # Create pending transaction record before going to Paystack
    # This ensures we have an audit trail even if the vendor closes the window
    transaction_obj = PaymentTransaction.objects.create(
        vendor=vendor,
        transaction_type=txn_type,
        amount=quote["amount_due"],
        currency='GHS',
        status='pending',
        paystack_reference=reference,
        plan=plan,
        billing=plan.billing_cycle,
    )

    payload = {
        "email": vendor.email,
        "amount": _pesewas(quote["amount_due"]),   # Paystack uses pesewas
        "currency": "GHS",
        "reference": reference,
        "callback_url": f"{settings.SITE_URL}/subscription/verify/?ref={reference}",
        "metadata": payment_metadata(
            vendor, plan, billing, quote, transaction_db_id=str(transaction_obj.id),
        ),
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

        return result

    except Exception as e:
        # Mark the pending transaction as failed
        transaction_obj.status = 'failed'
        transaction_obj.failure_reason = str(e)
        transaction_obj.save()
        logger.error(f"Paystack initiate failed for vendor {vendor.id}: {e}")
        raise


def charge_saved_card(vendor, plan_id: int, billing: str, card_id: int) -> dict:
    """
    Subscribe or upgrade by charging a card the store already saved — no
    redirect to Paystack. Activation goes through verify_and_activate so it
    shares the idempotency and locking of the redirect flow.
    """
    card = PaystackAuthorization.objects.filter(
        pk=card_id, vendor=vendor, is_reusable=True,
    ).first()
    if not card:
        raise SubscriptionChangeError("That card is no longer available. Choose another card.")

    plan, quote, early = prepare_plan_change(vendor, plan_id, billing)
    if early:
        return early

    reference = _generate_reference(vendor, "SUB")
    txn = PaymentTransaction.objects.create(
        vendor=vendor, authorization=card, transaction_type=quote["change_type"],
        amount=quote["amount_due"], currency='GHS', status='pending',
        paystack_reference=reference, plan=plan, billing=plan.billing_cycle,
    )

    try:
        response = requests.post(
            f"{PAYSTACK_BASE}/transaction/charge_authorization",
            json={
                "authorization_code": card.authorization_code,
                "email": vendor.email,
                "amount": _pesewas(quote["amount_due"]),
                "currency": "GHS",
                "reference": reference,
                "metadata": payment_metadata(vendor, plan, billing, quote, transaction_db_id=str(txn.id)),
            },
            headers=PAYSTACK_HEADERS,
            timeout=20,
        )
        ps_data = response.json().get("data") or {}
    except Exception as exc:
        txn.status, txn.failure_reason = 'failed', str(exc)
        txn.save(update_fields=['status', 'failure_reason'])
        logger.error(f"Saved-card charge failed for vendor {vendor.id}: {exc}")
        raise SubscriptionChangeError("We couldn't reach the payment provider. Please try again.")

    state = (ps_data.get("status") or "").lower()
    if state in ("send_otp", "send_pin", "open_url", "pending", "ongoing", "processing"):
        # Bank wants an extra step; the webhook activates the plan once it clears.
        return {"activated": False, "pending": True, "reference": reference,
                "message": "Your bank is confirming this payment. Your plan switches as soon as it does."}
    if state != "success":
        reason = ps_data.get("gateway_response") or "The card was declined."
        PaymentTransaction.objects.filter(pk=txn.pk, status='pending').update(status='failed', failure_reason=reason)
        raise SubscriptionChangeError(f"{reason} Try another card or Mobile Money.")

    from . import subscription_lifecycle as lifecycle
    try:
        sub = lifecycle.activate_paid_transaction(
            txn.pk, paid_pesewas=int(ps_data.get("amount") or 0),
            currency=ps_data.get("currency") or "GHS", gateway_id=ps_data.get("id", ""),
        )
    except lifecycle.ActivationRefused as exc:
        raise SubscriptionChangeError(str(exc))
    return {**lifecycle.subscription_result(sub), "activated": True, "reference": reference}


def _activate_free_plan(vendor, plan: SubscriptionPlan) -> dict:
    """Activates the free plan directly — no Paystack involved."""
    with transaction.atomic():
        # Cancel any existing active subscription
        # A lapsed/past-due paid plan stops here too: the store chose Free.
        VendorSubscription.objects.filter(
            vendor=vendor, status__in=VendorSubscription.LIVE_STATUSES
        ).update(status='cancelled')

        sub = VendorSubscription.objects.create(
            vendor=vendor,
            plan=plan,
            status='active',
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(days=36500),  # 100 years = forever
            auto_renew=False,
        )
        from .subscription_lifecycle import refresh_store
        refresh_store(vendor)

    return {"activated": True, "plan": plan.name}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Verify payment — called after Paystack redirects back
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# REPLACE verify_and_activate() in subscriptions/services.py with this version.
# Adds: idempotency check, select_for_update row lock, IntegrityError handling.
# ─────────────────────────────────────────────────────────────────────────────

class SubscriptionVerifyError(Exception):
    """A payment that must not activate anything; the message is safe to show."""


def _pesewas(amount) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _check_paid_transaction(txn, ps_data, meta):
    """
    The Paystack payment must be the one we started: same store, same currency,
    at least the amount we quoted. Anyone holding the public key can create a
    Paystack transaction with any metadata they like, so nothing about the
    store, plan or period is taken from a payment we did not initialise.
    """
    if txn is None or txn.vendor_id is None:
        raise SubscriptionVerifyError("We don't recognise this payment.")
    if str(meta.get("vendor_id", txn.vendor_id)) != str(txn.vendor_id):
        raise SubscriptionVerifyError("This payment belongs to a different store.")
    paid = int(ps_data.get("amount") or 0)
    if (ps_data.get("currency") or "").upper() != (txn.currency or "GHS").upper() \
            or paid + 1 < _pesewas(txn.amount):
        logger.warning("Subscription payment mismatch ref=%s paid=%s %s expected=%s %s",
                       txn.paystack_reference, paid, ps_data.get("currency"), txn.amount, txn.currency)
        raise SubscriptionVerifyError("The amount paid does not match this plan.")


def verify_and_activate(reference: str, vendor=None) -> dict:
    """
    Called from the /verify/ endpoint after Paystack redirects back.

    TRUSTED: only references we created (a PaymentTransaction row) activate,
    for the store that started them, and only once the amount we charged was
    paid. `vendor`, when given, is the caller's store and must match.

    IDEMPOTENT: a transaction is applied once. Replaying an old reference
    returns the subscription it created and never starts a new period —
    even after that subscription has ended.
    """
    from . import subscription_lifecycle as lifecycle

    txn_record = PaymentTransaction.objects.filter(paystack_reference=reference).first()
    if txn_record is None or txn_record.vendor_id is None:
        raise SubscriptionVerifyError("We don't recognise this payment.")
    if vendor is not None and txn_record.vendor_id != vendor.id:
        raise SubscriptionVerifyError("This payment belongs to a different store.")

    if txn_record.status == 'success' and txn_record.subscription_id:
        logger.info(f'verify_and_activate: ref={reference} already applied')
        return lifecycle.subscription_result(txn_record.subscription)
    if txn_record.status in ('review', 'refunded'):
        raise SubscriptionVerifyError(
            "This payment is being checked by our team. We'll be in touch — no need to pay again.")

    if txn_record.transaction_type == 'renewal':
        outcome = lifecycle.reconcile_renewal(txn_record)
        if outcome != 'applied':
            raise SubscriptionVerifyError("This renewal payment hasn't completed.")
        txn_record.refresh_from_db()
        return lifecycle.subscription_result(txn_record.subscription)
    if txn_record.transaction_type not in lifecycle.PLAN_PURCHASE_TYPES:
        raise SubscriptionVerifyError("We don't recognise this payment.")

    # ── Fetch + verify from Paystack ─────────────────────────────────────────
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
        PaymentTransaction.objects.filter(paystack_reference=reference, status='pending').update(
            status='failed',
            failure_reason=ps_data.get("gateway_response", "Payment not successful"),
        )
        raise Exception("Payment was not successful.")

    meta = ps_data.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    if ps_data.get("reference") not in (None, reference):
        raise SubscriptionVerifyError("We don't recognise this payment.")
    _check_paid_transaction(txn_record, ps_data, meta)

    try:
        sub = lifecycle.activate_paid_transaction(
            txn_record.pk,
            paid_pesewas=int(ps_data.get("amount") or 0),
            currency=ps_data.get("currency") or "GHS",
            gateway_id=ps_data.get("id", ""),
            authorization=ps_data.get("authorization") or {},
            customer_code=(ps_data.get("customer") or {}).get("customer_code"),
            legacy_plan_id=meta.get("plan_id"),
            legacy_billing=meta.get("billing"),
        )
    except lifecycle.ActivationRefused as exc:
        raise SubscriptionVerifyError(str(exc))
    return lifecycle.subscription_result(sub)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Renewal — see payments/subscription_lifecycle.py
# ─────────────────────────────────────────────────────────────────────────────

def charge_for_renewal(subscription_id: int, manual: bool = False, card=None) -> dict:
    """
    Collect the renewal for one subscription (saved card first, then the
    default MoMo account). Claims the subscription under a lock, so running
    it twice never charges twice. manual=True is the seller's "Pay now".
    """
    from .subscription_lifecycle import attempt_renewal
    return attempt_renewal(subscription_id, manual=manual, card=card)


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
            vendor=vendor, plan__price__gt=0,
        ).filter(
            models.Q(status='active', end_date__gt=timezone.now()) | models.Q(status='past_due')
        ).select_for_update().first()

        if not sub:
            raise SubscriptionChangeError("No active paid subscription found.")
        if sub.cancel_at_period_end:
            raise SubscriptionChangeError("This subscription is already cancelled.")

        sub.cancel(reason=reason)
        if sub.status == 'past_due':
            # Nothing left to keep: the paid period is over and retries stop.
            from .subscription_lifecycle import _expire
            _expire(sub)

    # Fire the email outside the transaction — no need to hold the lock
    # while Celery enqueues the task.
    from .email_tasks import send_cancellation_email

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
        # Our record of the reference: only card adds we started, for this
        # store, are saved (the metadata alone can be forged client-side).
        from django.core.cache import cache
        cache.set(f"{CARD_ADD_KEY}{reference}", vendor.id, CARD_ADD_TTL)
        return {
            "authorization_url": data["data"]["authorization_url"],
            "reference":         reference,
            "access_code":       data["data"]["access_code"],
        }
 
    except Exception as exc:
        logger.error(f"initiate_card_add failed for vendor {vendor.id}: {exc}")
        raise
 
 
CARD_ADD_KEY = 'card_add_ref:'
CARD_ADD_TTL = 2 * 60 * 60


def verify_card_add(reference: str, vendor=None) -> dict:
    """
    Called after Paystack redirects back from the card tokenization charge.
 
    1. Verifies the GHS 1.00 charge succeeded at Paystack
    2. Saves the reusable authorization (card token) to PaystackAuthorization
    3. Refunds the GHS 1.00 so the vendor isn't actually charged
    4. Returns { card_saved: bool, last4, card_type, expiry_display }
 
    If a card with this authorization_code already exists (duplicate), returns
    { card_saved: False, reason: 'duplicate' } without raising.

    Only references initiate_card_add started (and, when `vendor` is given,
    started by that store) are accepted.
    """
    from django.core.cache import cache
    started_by = cache.get(f"{CARD_ADD_KEY}{reference}")
    if started_by is None or (vendor is not None and started_by != vendor.id):
        raise SubscriptionVerifyError("We don't recognise this card check. Please add the card again.")
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
        raise SubscriptionVerifyError("Card charge was not successful — card not saved.")
 
    meta = ps_data.get("metadata") or {}
    if meta.get("purpose") != "card_add":
        # This reference is for a subscription, not a card add — don't process here
        raise SubscriptionVerifyError("Reference is not a card-add transaction.")
 
    from vendor.models import Vendor
    if str(meta.get("vendor_id")) != str(started_by):
        raise SubscriptionVerifyError("We don't recognise this card check. Please add the card again.")
    vendor = Vendor.objects.get(pk=started_by)
 
    auth_data = ps_data.get("authorization", {})
    auth_code = auth_data.get("authorization_code", "")
 
    if not auth_code or not auth_data.get("reusable"):
        raise SubscriptionVerifyError("Card is not reusable — it cannot be saved for future charges.")
 
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
    """
    The signed event is the source of truth when the seller closes the tab
    before our verify page loads, or a MoMo/OTP payment clears later.
    """
    from . import subscription_lifecycle as lifecycle

    reference = data.get("reference")
    txn = PaymentTransaction.objects.filter(paystack_reference=reference).first()
    if not txn:
        return f"charge.success: unknown reference {reference}"
    paid = dict(paid_pesewas=int(data.get("amount") or 0), currency=data.get("currency") or "GHS",
                gateway_id=data.get("id", ""))

    if txn.transaction_type == 'renewal':
        sub = lifecycle.apply_renewal_payment(txn.pk, **paid)
        return f"charge.success: renewal {'applied' if sub else 'not applied'} {reference}"

    if txn.transaction_type in lifecycle.PLAN_PURCHASE_TYPES:
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        try:
            lifecycle.activate_paid_transaction(
                txn.pk, authorization=data.get("authorization") or {},
                customer_code=(data.get("customer") or {}).get("customer_code"),
                legacy_plan_id=meta.get("plan_id"), legacy_billing=meta.get("billing"), **paid)
        except lifecycle.ActivationRefused as exc:
            return f"charge.success: not applied {reference}: {exc}"
        return f"charge.success: plan applied {reference}"

    # Other payment types (sponsored-products card charges) settle in their own flow.
    if txn.status == 'pending':
        PaymentTransaction.objects.filter(pk=txn.pk, status='pending').update(
            status='success', paystack_transaction_id=str(data.get("id", "")), paid_at=timezone.now())
    return f"charge.success: processed {reference}"


def _webhook_charge_failed(data: dict) -> str:
    reference = data.get("reference")
    # Never let a late/forged failure overwrite a payment that succeeded.
    PaymentTransaction.objects.filter(paystack_reference=reference, status='pending').update(
        status='failed',
        failure_reason=data.get("gateway_response", "Charge failed"),
    )
    return f"charge.failed: {reference}"


def _webhook_invoice_failed(data: dict) -> str:
    """Triggered when a recurring charge fails on Paystack's side."""
    reference = data.get("reference", "")
    PaymentTransaction.objects.filter(paystack_reference=reference, status='pending').update(
        status='failed',
        failure_reason="Invoice payment failed",
    )
    return f"invoice.payment_failed: {reference}"


def _webhook_refund_processed(data: dict) -> str:
    from .subscription_lifecycle import handle_refund
    reference = data.get("transaction_reference", "")
    amount = data.get("amount")
    outcome = handle_refund(reference, int(amount) if amount is not None else None)
    return f"refund.processed: {reference} ({outcome})"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def period_end_from(meta: dict, billing: str):
    """
    End date for a newly paid subscription: the quoted period_end carried in
    the payment metadata (prorated upgrades keep the old renewal date), or a
    full billing period from now for older payments without one.
    """
    period_end = parse_datetime(meta.get("period_end") or "") if meta else None
    if period_end and period_end > timezone.now():
        return period_end
    return timezone.now() + timedelta(days=BILLING_DAYS.get(billing, 30))


def _generate_reference(vendor, prefix: str) -> str:
    uid = uuid.uuid4().hex[:10].upper()
    return f"{prefix}-{vendor.id}-{uid}"


def _ensure_usage_record(vendor, subscription: VendorSubscription = None):
    """Bring the billing-page usage numbers in line with reality (never resets them)."""
    from .entitlements import sync_usage
    return sync_usage(vendor)


def _sync_vendor_flags(vendor):
    """
    Keeps the denormalized is_subscribed, subscription_start_date,
    subscription_end_date fields on the Vendor model in sync.
    """
    active_sub = VendorSubscription.objects.filter(
        vendor=vendor, status__in=VendorSubscription.LIVE_STATUSES
    ).order_by('-created_at').first()

    if active_sub and active_sub.in_force():
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
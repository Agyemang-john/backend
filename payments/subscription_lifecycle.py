"""
payments/subscription_lifecycle.py
The seller subscription lifecycle in one place: activating paid plans,
renewing them, retrying failed renewals (dunning), the grace period, expiry
and reminders.

Rules that keep it tight:

* One payment applies once. A PaymentTransaction is locked (SELECT … FOR
  UPDATE) while it is applied and is only ever applied while 'pending'; a
  'success' transaction is never applied again, however often a verify URL,
  poll or webhook is replayed.
* What a payment buys is stored on our PaymentTransaction (plan, billing,
  period_end) when we start it, never read from gateway metadata or a cache.
* Prices are re-quoted when the money arrives, inside the store lock. A
  payment that no longer covers what is due (stale quote, duplicate, plan
  changed) is parked as 'review' for a person instead of being applied.
* A renewal is tied to the period it renews (period_end). Paying twice for
  the same period can't extend the plan twice.
* Renewal retries are state in the database (renewal_attempts,
  next_renewal_attempt_at), claimed under a row lock. Nothing is scheduled
  with long Celery countdowns, which a Redis broker re-delivers after its
  visibility timeout (= double charges).
* Plans are only in force while their dates say so (VendorSubscription.in_force);
  the hourly sweep just tidies statuses and sends the emails.

Sweeps select ids only and process each row in its own short transaction
with skip_locked, so they scale with the number of rows that are due, and
two workers never process the same subscription.
"""
import logging
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    PaymentTransaction, PaystackAuthorization, PaystackCustomer,
    SubscriptionPlan, VendorSubscription,
)

logger = logging.getLogger(__name__)

LIVE = VendorSubscription.LIVE_STATUSES
# Payment types that buy (or change to) a plan, applied by activate_paid_transaction.
PLAN_PURCHASE_TYPES = ('initial', 'upgrade', 'manual')
# Gateway states that mean "still in progress, don't charge again yet".
GATEWAY_PENDING = ('ongoing', 'pending', 'processing', 'send_otp', 'send_pin', 'send_phone',
                   'send_birthday', 'send_address', 'open_url', 'pay_offline', 'queued')
GATEWAY_FAILED = ('failed', 'abandoned', 'reversed')
# A manual "Pay now" waits this long for an in-flight payment before allowing another.
MANUAL_INFLIGHT_WINDOW = timedelta(minutes=10)


class ActivationRefused(Exception):
    """A payment that must not change the plan. The message is safe to show the seller."""


class PaymentParked(ActivationRefused):
    """Money arrived but couldn't be applied; the transaction is now 'review'."""


class RenewalError(Exception):
    """A manual renewal can't start. Message is safe to show."""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def config():
    from .email_models import SubscriptionEmailConfig
    return SubscriptionEmailConfig.get()


def max_attempts(cfg=None):
    cfg = cfg or config()
    return 1 + cfg.renewal_max_retries


def pesewas(amount) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _paystack():
    from .services import PAYSTACK_BASE, PAYSTACK_HEADERS
    return PAYSTACK_BASE, PAYSTACK_HEADERS


def _after_commit(task, *args, **kwargs):
    """Queue a Celery task once the surrounding transaction has committed."""
    transaction.on_commit(lambda: task.delay(*args, **kwargs))


def refresh_store(vendor):
    """Re-derive everything cached about the store's plan after a change."""
    from . import entitlements
    from .services import _sync_vendor_flags
    entitlements.forget_plan(vendor)
    _sync_vendor_flags(vendor)
    entitlements.sync_usage(vendor)


def lapse_date(sub, cfg=None):
    """When the store loses this plan if the renewal never succeeds."""
    if sub.status == 'past_due' and sub.grace_until:
        return sub.grace_until
    if not sub.auto_renew:
        return sub.end_date
    cfg = cfg or config()
    return sub.end_date + timedelta(days=cfg.grace_period_days)


def _park(txn, reason, *, gateway_id='', paid_pesewas=None):
    """Record money we received but won't apply automatically."""
    txn.status = 'review'
    txn.failure_reason = reason[:1000]
    txn.paid_at = txn.paid_at or timezone.now()
    if gateway_id:
        txn.paystack_transaction_id = str(gateway_id)
    txn.save(update_fields=['status', 'failure_reason', 'paid_at', 'paystack_transaction_id'])
    logger.error(
        "SUBSCRIPTION PAYMENT NEEDS REVIEW ref=%s vendor=%s amount=%s paid_pesewas=%s: %s",
        txn.paystack_reference, txn.vendor_id, txn.amount, paid_pesewas, reason,
    )


def _save_card(vendor, auth_data, customer_code):
    """Keep a reusable card from a successful checkout for renewals."""
    if not auth_data or not auth_data.get('reusable') or not auth_data.get('authorization_code'):
        return None
    customer = PaystackCustomer.objects.filter(vendor=vendor).first()
    if customer is None:
        if not customer_code:
            return None
        customer, _ = PaystackCustomer.objects.get_or_create(
            vendor=vendor, defaults={'customer_code': customer_code, 'email': vendor.email})
    card, created = PaystackAuthorization.objects.get_or_create(
        authorization_code=auth_data['authorization_code'],
        defaults={
            'vendor': vendor, 'paystack_customer': customer,
            'card_type': auth_data.get('card_type', ''), 'last4': auth_data.get('last4', ''),
            'exp_month': auth_data.get('exp_month', ''), 'exp_year': auth_data.get('exp_year', ''),
            'bank': auth_data.get('bank', ''), 'is_reusable': True, 'is_default': True,
        },
    )
    if card.vendor_id != vendor.pk:
        # A card token is tied to one Paystack customer; never move it between stores.
        return None
    if created:
        PaystackAuthorization.objects.filter(vendor=vendor).exclude(pk=card.pk).update(is_default=False)
    return card


def subscription_result(sub):
    return {
        'vendor_id': sub.vendor_id,
        'plan':      sub.plan.name,
        'status':    sub.status,
        'end_date':  sub.end_date.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Activating a paid plan (new subscription or upgrade)
# ─────────────────────────────────────────────────────────────────────────────

def activate_paid_transaction(txn_id, *, paid_pesewas, currency='GHS', gateway_id='',
                              authorization=None, customer_code=None, legacy_plan_id=None,
                              legacy_billing=None):
    """
    Apply a successful plan purchase. Safe to call any number of times, from
    any path (verify redirect, saved card, MoMo poll, webhook): the first call
    applies it, the rest return the same subscription.

    Raises ActivationRefused when the payment doesn't match what we charged,
    and PaymentParked (after saving the transaction as 'review') when money
    arrived but the change is no longer valid.
    """
    from vendor.models import Vendor
    from .services import quote_change

    parked_reason = None
    with transaction.atomic():
        txn = (PaymentTransaction.objects.select_for_update(of=('self',))
               .select_related('subscription__plan').get(pk=txn_id))
        if txn.status == 'success' and txn.subscription_id:
            return txn.subscription
        if txn.transaction_type not in PLAN_PURCHASE_TYPES:
            raise ActivationRefused("This payment is not for a plan.")
        if txn.status in ('review', 'refunded'):
            raise ActivationRefused(
                "This payment is being checked by our team. We'll be in touch — no need to pay again.")
        if (currency or 'GHS').upper() != (txn.currency or 'GHS').upper() or paid_pesewas + 1 < pesewas(txn.amount):
            logger.warning("Plan payment mismatch ref=%s paid=%s %s expected=%s %s",
                           txn.paystack_reference, paid_pesewas, currency, txn.amount, txn.currency)
            raise ActivationRefused("The amount paid does not match this plan.")

        vendor = Vendor.objects.select_for_update(of=('self',)).get(pk=txn.vendor_id)
        plan = txn.plan
        if plan is None and legacy_plan_id:
            # Payments started before plans were recorded on the transaction.
            plan = SubscriptionPlan.objects.filter(pk=legacy_plan_id).first()
        if plan is None:
            parked_reason = "Paid, but the payment has no plan attached."
        else:
            # Re-quote now, under the lock: the store's subscription may have
            # changed since checkout started (renewed, upgraded, lapsed).
            quote = quote_change(vendor, plan)
            if quote['change_type'] not in ('initial', 'upgrade'):
                parked_reason = (f"Paid for {plan.name}, but that is now a '{quote['change_type']}' "
                                 f"for this store (already on it, or it would be a downgrade).")
            elif paid_pesewas + 1 < pesewas(quote['amount_due']):
                parked_reason = (f"Paid {Decimal(paid_pesewas) / 100} for {plan.name}, but "
                                 f"{quote['amount_due']} is due now (the quote changed before payment).")

        if parked_reason:
            _park(txn, parked_reason, gateway_id=gateway_id, paid_pesewas=paid_pesewas)
        else:
            VendorSubscription.objects.filter(vendor=vendor, status__in=LIVE).update(status='cancelled')
            sub = VendorSubscription.objects.create(
                vendor=vendor, plan=plan, status='active',
                start_date=timezone.now(), end_date=quote['period_end'],
                auto_renew=True, payment_reference=txn.paystack_reference,
            )
            _save_card(vendor, authorization, customer_code)
            txn.status = 'success'
            txn.subscription = sub
            txn.plan = plan
            txn.billing = txn.billing or legacy_billing or plan.billing_cycle
            txn.paystack_transaction_id = str(gateway_id or txn.paystack_transaction_id or '')
            txn.paid_at = timezone.now()
            txn.save(update_fields=['status', 'subscription', 'plan', 'billing',
                                    'paystack_transaction_id', 'paid_at'])
            refresh_store(vendor)
            from .email_tasks import send_subscription_confirmation_email
            _after_commit(send_subscription_confirmation_email, vendor.id, sub.id)

    if parked_reason:
        raise PaymentParked(
            "We received your payment but couldn't apply it automatically. "
            "Our team has been alerted and will apply or refund it — please don't pay again.")
    logger.info("Plan activated: vendor=%s plan=%s ref=%s", sub.vendor_id, sub.plan.name, txn.paystack_reference)
    return sub


# ─────────────────────────────────────────────────────────────────────────────
# 2. Applying a renewal payment
# ─────────────────────────────────────────────────────────────────────────────

def apply_renewal_payment(txn_id, *, paid_pesewas, currency='GHS', gateway_id=''):
    """
    Extend the subscription a renewal payment was for. Idempotent per
    transaction, and a second payment for the same period is parked instead
    of extending twice. Returns the subscription, or None if parked/ignored.
    """
    parked = False
    with transaction.atomic():
        txn = PaymentTransaction.objects.select_for_update(of=('self',)).get(pk=txn_id)
        if txn.status == 'success':
            return txn.subscription
        if txn.status in ('review', 'refunded') or not txn.subscription_id:
            return None
        sub = (VendorSubscription.objects.select_for_update(of=('self',))
               .select_related('plan', 'scheduled_plan', 'vendor').get(pk=txn.subscription_id))

        if (currency or 'GHS').upper() != (txn.currency or 'GHS').upper() or paid_pesewas + 1 < pesewas(txn.amount):
            _park(txn, f"Renewal underpaid or wrong currency ({paid_pesewas} {currency}).",
                  gateway_id=gateway_id, paid_pesewas=paid_pesewas)
            parked = True
        else:
            plan = txn.plan or sub.scheduled_plan or sub.plan
            same_period = txn.period_end is None or txn.period_end == sub.end_date
            other_live = VendorSubscription.objects.filter(
                vendor_id=sub.vendor_id, status__in=LIVE).exclude(pk=sub.pk).exists()
            if sub.status in ('active', 'past_due') and same_period:
                sub.renew(plan)
            elif sub.status == 'expired' and not other_live:
                # Paid after the plan lapsed (e.g. a late Mobile Money approval):
                # restart it from today rather than keep their money.
                sub.renew(plan)
            else:
                _park(txn, "Renewal paid for a period that was already renewed, or the plan changed since.",
                      gateway_id=gateway_id, paid_pesewas=paid_pesewas)
                parked = True

        if not parked:
            txn.status = 'success'
            txn.paystack_transaction_id = str(gateway_id or txn.paystack_transaction_id or '')
            txn.paid_at = timezone.now()
            txn.save(update_fields=['status', 'paystack_transaction_id', 'paid_at'])
            refresh_store(sub.vendor)
            from .email_tasks import send_renewal_success_email
            _after_commit(send_renewal_success_email, sub.vendor_id)

    if parked:
        return None
    logger.info("Renewal applied: sub=%s ref=%s end=%s", sub.pk, txn.paystack_reference, sub.end_date)
    return sub


def _gateway_verify(reference):
    """(state, data) for a reference at Paystack: 'success' | 'failed' | 'pending' | 'missing'."""
    base, headers = _paystack()
    resp = requests.get(f"{base}/transaction/verify/{reference}", headers=headers, timeout=15)
    body = resp.json() if resp.content else {}
    data = body.get('data') or {}
    if resp.status_code == 404 or (not body.get('status') and not data):
        return 'missing', data
    status = (data.get('status') or '').lower()
    if status == 'success':
        return 'success', data
    if status in GATEWAY_FAILED:
        return 'failed', data
    return 'pending', data


def reconcile_renewal(txn):
    """
    Settle a renewal transaction left 'pending' (timeout, crash, unapproved
    MoMo prompt) by asking Paystack. Returns 'applied', 'failed' or 'pending'.
    """
    try:
        state, data = _gateway_verify(txn.paystack_reference)
    except Exception as exc:  # network: try again next run
        logger.warning("reconcile_renewal: verify failed ref=%s: %s", txn.paystack_reference, exc)
        return 'pending'
    if state == 'success':
        sub = apply_renewal_payment(txn.pk, paid_pesewas=int(data.get('amount') or 0),
                                    currency=data.get('currency') or 'GHS', gateway_id=data.get('id', ''))
        return 'applied' if sub else 'failed'
    if state in ('failed', 'missing'):
        PaymentTransaction.objects.filter(pk=txn.pk, status='pending').update(
            status='failed', failure_reason=(data.get('gateway_response') or 'Not completed')[:1000])
        return 'failed'
    return 'pending'


# ─────────────────────────────────────────────────────────────────────────────
# 3. Renewal attempts (automatic and "Pay now")
# ─────────────────────────────────────────────────────────────────────────────

def _due_filter(now, cfg):
    window = now + timedelta(days=cfg.renewal_advance_days)
    return (
        Q(auto_renew=True, renewal_attempts__lt=max_attempts(cfg))
        & (Q(next_renewal_attempt_at__isnull=True) | Q(next_renewal_attempt_at__lte=now))
        & (Q(status='active', end_date__lte=window, end_date__gt=now - timedelta(days=cfg.grace_period_days))
           | Q(status='past_due', grace_until__gt=now))
    )


def due_for_renewal_ids(now=None):
    """Ids of subscriptions whose renewal charge should run now."""
    now = now or timezone.now()
    cfg = config()
    return (VendorSubscription.objects.filter(_due_filter(now, cfg))
            .exclude(plan__price__lte=0, scheduled_plan__isnull=True)
            .values_list('id', flat=True))


def _default_card(vendor_id, by_date=None):
    """The store's default reusable card, if it hasn't expired (by `by_date`)."""
    card = PaystackAuthorization.objects.filter(
        vendor_id=vendor_id, is_default=True, is_reusable=True).first()
    if card and by_date and not card_valid_until(card, by_date):
        return None
    return card


def card_valid_until(card, when):
    """True if the card's expiry month is not before `when`'s month."""
    try:
        y, m = int(card.exp_year), int(card.exp_month)
    except (TypeError, ValueError):
        return True  # unknown expiry: let Paystack decide
    if y < 100:
        y += 2000
    return (y, m) >= (when.year, when.month)


def _default_momo(vendor_id):
    from .momo_models import MomoAccount
    return MomoAccount.objects.filter(vendor_id=vendor_id, is_default=True).first()


def attempt_renewal(sub_id, *, manual=False, card=None):
    """
    Try to collect the renewal for one subscription.

    Automatic runs claim the subscription under a row lock (skip_locked) and
    stamp the next attempt time before calling Paystack, so duplicate tasks
    or overlapping workers can't charge twice. A transaction left pending by
    an earlier attempt is settled first; if it's still in progress at the
    gateway, no new charge is made.

    manual=True is the seller's "Pay now" (card): no schedule check, but it
    refuses while another payment for this period is in flight.
    """
    now = timezone.now()
    cfg = config()
    with transaction.atomic():
        qs = VendorSubscription.objects.select_related('plan', 'scheduled_plan', 'vendor')
        sub = (qs.select_for_update(of=('self',)) if manual else qs.select_for_update(skip_locked=True, of=('self',))) \
            .filter(pk=sub_id).first()
        if sub is None:
            return {'status': 'busy'}
        if manual:
            if sub.status not in ('active', 'past_due') or not sub.in_force(now):
                raise RenewalError("Your plan has already ended. Choose a plan to subscribe again.")
            recent = PaymentTransaction.objects.filter(
                subscription=sub, transaction_type='renewal', status='pending',
                created_at__gte=now - MANUAL_INFLIGHT_WINDOW).exists()
            if recent:
                raise RenewalError("A payment for this renewal is already in progress. "
                                   "Give it a few minutes before trying again.")
        else:
            if not VendorSubscription.objects.filter(pk=sub.pk).filter(_due_filter(now, cfg)).exists():
                return {'status': 'not_due'}
            sub.renewal_attempts += 1
            sub.next_renewal_attempt_at = now + timedelta(hours=cfg.renewal_retry_hours)
            sub.save(update_fields=['renewal_attempts', 'next_renewal_attempt_at', 'updated_at'])
        plan = sub.scheduled_plan or sub.plan
        period_end = sub.end_date
        attempt = sub.renewal_attempts
        pending = list(PaymentTransaction.objects.filter(
            subscription=sub, transaction_type='renewal', status='pending'))

    if plan.price <= 0:
        return {'status': 'nothing_to_charge'}

    # Settle anything an earlier attempt left in flight before charging again.
    for txn in pending:
        outcome = reconcile_renewal(txn)
        if outcome == 'applied':
            return {'status': 'success', 'reference': txn.paystack_reference, 'method': 'reconciled'}
        if outcome == 'pending':
            return {'status': 'pending', 'reference': txn.paystack_reference}

    card = card or _default_card(sub.vendor_id, by_date=now)
    if card:
        return _charge_card(sub, plan, card, period_end, attempt, manual)
    if not manual:
        momo = _default_momo(sub.vendor_id)
        if momo:
            return _charge_momo(sub, plan, momo, period_end, attempt)
    record_renewal_failure(sub.pk, "No working card or Mobile Money account on file.", no_method=True)
    return {'status': 'failed', 'reason': 'no_payment_method'}


def _renewal_reference(sub, period_end, attempt, prefix, manual):
    # Automatic attempts get a deterministic reference: a duplicate run of
    # the same attempt collides on the unique reference instead of charging.
    if manual:
        return f"{prefix}-{sub.vendor_id}-{sub.pk}-{uuid.uuid4().hex[:8].upper()}"
    return f"{prefix}-{sub.vendor_id}-{sub.pk}-{period_end:%Y%m%d%H%M}-{attempt}"


def _new_renewal_txn(sub, plan, period_end, reference, authorization=None):
    try:
        with transaction.atomic():
            return PaymentTransaction.objects.create(
                vendor_id=sub.vendor_id, subscription=sub, authorization=authorization,
                transaction_type='renewal', amount=plan.price, currency='GHS', status='pending',
                paystack_reference=reference, plan=plan, billing=plan.billing_cycle, period_end=period_end,
            )
    except IntegrityError:
        return None  # this exact attempt already ran


def _charge_card(sub, plan, card, period_end, attempt, manual):
    base, headers = _paystack()
    reference = _renewal_reference(sub, period_end, attempt, 'RNW', manual)
    txn = _new_renewal_txn(sub, plan, period_end, reference, authorization=card)
    if txn is None:
        return {'status': 'duplicate', 'reference': reference}
    try:
        resp = requests.post(
            f"{base}/transaction/charge_authorization", headers=headers, timeout=20,
            json={
                'authorization_code': card.authorization_code,
                'email': sub.vendor.email,
                'amount': pesewas(plan.price),
                'currency': 'GHS',
                'reference': reference,
                'metadata': {'payment_type': 'renewal', 'vendor_id': str(sub.vendor_id),
                             'subscription_id': str(sub.pk)},
            },
        )
        body = resp.json()
    except Exception as exc:
        # We don't know if Paystack charged: leave it pending; the next run
        # (or the webhook) settles it before any new charge.
        logger.warning("Renewal card charge unknown ref=%s: %s", reference, exc)
        return {'status': 'unknown', 'reference': reference}

    data = body.get('data') or {}
    state = (data.get('status') or '').lower()
    if state == 'success':
        applied = apply_renewal_payment(txn.pk, paid_pesewas=int(data.get('amount') or 0),
                                        currency=data.get('currency') or 'GHS', gateway_id=data.get('id', ''))
        if applied:
            return {'status': 'success', 'method': 'card', 'reference': reference}
        return {'status': 'review', 'reference': reference}
    if state in GATEWAY_PENDING:
        return {'status': 'pending', 'reference': reference}

    reason = data.get('gateway_response') or body.get('message') or 'The card was declined.'
    PaymentTransaction.objects.filter(pk=txn.pk, status='pending').update(status='failed', failure_reason=reason[:1000])
    if manual:
        raise RenewalError(f"{reason} Try another card or Mobile Money.")
    record_renewal_failure(sub.pk, reason)
    return {'status': 'failed', 'reason': reason, 'reference': reference}


def _charge_momo(sub, plan, momo, period_end, attempt):
    from .momo_services import PROVIDER_PAYSTACK_MAP, _post_charge, _mask, require_billing_profile
    try:
        profile = require_billing_profile(sub.vendor)
    except ValueError:
        record_renewal_failure(sub.pk, "Billing profile incomplete, so Mobile Money can't be charged.", no_method=True)
        return {'status': 'failed', 'reason': 'billing_profile_incomplete'}

    reference = _renewal_reference(sub, period_end, attempt, 'RNW-MM', manual=False)
    txn = _new_renewal_txn(sub, plan, period_end, reference)
    if txn is None:
        return {'status': 'duplicate', 'reference': reference}
    try:
        resp, data = _post_charge(reference, pesewas(plan.price), profile.email, momo.phone,
                                  PROVIDER_PAYSTACK_MAP.get(momo.provider, momo.provider))
    except Exception as exc:
        logger.warning("Renewal MoMo charge unknown ref=%s: %s", reference, exc)
        return {'status': 'unknown', 'reference': reference}
    if resp.status_code >= 400 or not data.get('status'):
        reason = data.get('message') or 'Mobile Money charge failed.'
        PaymentTransaction.objects.filter(pk=txn.pk, status='pending').update(status='failed', failure_reason=reason[:1000])
        record_renewal_failure(sub.pk, reason)
        return {'status': 'failed', 'reason': reason}
    momo.last_reference = reference
    momo.save(update_fields=['last_reference'])
    charge = data.get('data') or {}
    return {
        'status': 'pending_momo', 'method': 'momo', 'reference': reference,
        'display_text': charge.get('display_text') or 'Please approve the renewal on your phone.',
        'requires_otp': charge.get('status') == 'send_otp', 'masked_phone': _mask(momo.phone),
    }


def record_renewal_failure(sub_id, reason, *, no_method=False):
    """Note a failed attempt and tell the seller (once per attempt)."""
    cfg = config()
    with transaction.atomic():
        sub = VendorSubscription.objects.select_for_update(of=('self',)).select_related('plan').filter(pk=sub_id).first()
        if sub is None or sub.status not in ('active', 'past_due'):
            return
        sub.last_renewal_error = (reason or '')[:255]
        attempts_left = max(max_attempts(cfg) - sub.renewal_attempts, 0)
        key = f"fail{sub.renewal_attempts}:{sub.end_date:%Y%m%d}"
        send = sub.last_reminder_key != key
        if send:
            sub.last_reminder_key = key
        sub.save(update_fields=['last_renewal_error', 'last_reminder_key', 'updated_at'])
        if send:
            from .email_tasks import send_payment_method_required_email
            next_try = sub.next_renewal_attempt_at if attempts_left else None
            _after_commit(
                send_payment_method_required_email, sub.vendor_id,
                reason=reason, no_method=no_method,
                access_until=lapse_date(sub, cfg).isoformat(),
                next_attempt=next_try.isoformat() if next_try else None,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Lapsing: past_due (grace) and expiry — hourly sweep
# ─────────────────────────────────────────────────────────────────────────────

def _expire(sub):
    sub.status = 'expired'
    sub.grace_until = None
    sub.next_renewal_attempt_at = None
    sub.save(update_fields=['status', 'grace_until', 'next_renewal_attempt_at', 'updated_at'])
    refresh_store(sub.vendor)
    from .email_tasks import send_subscription_expired_email
    _after_commit(send_subscription_expired_email, sub.vendor_id)


def _lock_one(pk, **conditions):
    return (VendorSubscription.objects.select_for_update(skip_locked=True, of=('self',))
            .select_related('plan', 'vendor').filter(pk=pk, **conditions).first())


def sweep_lapsed(now=None):
    """
    Move subscriptions whose time is up to past_due (grace) or expired.
    Returns {'past_due': n, 'expired': n}.
    """
    now = now or timezone.now()
    cfg = config()
    grace = timedelta(days=cfg.grace_period_days)
    counts = {'past_due': 0, 'expired': 0}

    # Paid period over.
    for pk in list(VendorSubscription.objects.filter(status='active', end_date__lte=now).values_list('pk', flat=True)):
        with transaction.atomic():
            sub = _lock_one(pk, status='active', end_date__lte=now)
            if sub is None:
                continue
            if sub.plan.price > 0 and sub.auto_renew and sub.end_date + grace > now:
                sub.status = 'past_due'
                sub.grace_until = sub.end_date + grace
                key = f"due:{sub.end_date:%Y%m%d}"
                send = not sub.last_reminder_key.startswith('fail') or not sub.last_reminder_key.endswith(
                    f":{sub.end_date:%Y%m%d}")
                if send:
                    sub.last_reminder_key = key
                sub.save(update_fields=['status', 'grace_until', 'last_reminder_key', 'updated_at'])
                refresh_store(sub.vendor)
                if send:
                    from .email_tasks import send_payment_method_required_email
                    _after_commit(send_payment_method_required_email, sub.vendor_id,
                                  reason=sub.last_renewal_error or "Your renewal payment hasn't gone through yet.",
                                  access_until=sub.grace_until.isoformat(), next_attempt=None)
                counts['past_due'] += 1
            else:
                _expire(sub)
                counts['expired'] += 1

    # Grace over, renewal switched off, or legacy past_due rows without a grace date.
    ended = VendorSubscription.objects.filter(status='past_due').filter(
        Q(grace_until__lte=now) | Q(auto_renew=False)
        | Q(grace_until__isnull=True, end_date__lte=now - grace))
    for pk in list(ended.values_list('pk', flat=True)):
        with transaction.atomic():
            sub = _lock_one(pk, status='past_due')
            if sub is not None:
                _expire(sub)
                counts['expired'] += 1
    VendorSubscription.objects.filter(status='past_due', grace_until__isnull=True).update(
        grace_until=now + grace)

    # Trials that ran out.
    trials = VendorSubscription.objects.filter(status='trial').filter(
        Q(end_date__lte=now) | Q(trial_end_date__lte=now))
    for pk in list(trials.values_list('pk', flat=True)):
        with transaction.atomic():
            sub = _lock_one(pk, status='trial')
            if sub is not None:
                _expire(sub)
                counts['expired'] += 1
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# 5. Reminders before the renewal date — daily
# ─────────────────────────────────────────────────────────────────────────────

def has_payment_method(vendor_id, by_date):
    return bool(_default_card(vendor_id, by_date=by_date) or _default_momo(vendor_id))


def send_upcoming_reminders(now=None):
    """
    Each reminder is sent at most once per period (last_reminder_key) and
    selection is by window, not exact date, so a missed run catches up.

    auto_renew off → "your plan ends on X" at each threshold (7, 3 days).
    auto_renew on  → one heads-up at the first threshold; if there is no
                     usable card/MoMo for the renewal date, a "add a payment
                     method" reminder at each threshold instead.
    """
    from .email_tasks import send_expiring_soon_email, send_payment_method_required_email
    now = now or timezone.now()
    cfg = config()
    thresholds = sorted({d for d in (cfg.expiry_warning_days, cfg.second_warning_days) if d > 0})
    if not thresholds:
        return 0
    sent = 0
    qs = (VendorSubscription.objects.filter(
        status='active', plan__price__gt=0, end_date__gt=now,
        end_date__lte=now + timedelta(days=max(thresholds)))
        .select_related('plan'))
    for sub in qs.iterator(chunk_size=500):
        t = next(d for d in thresholds if sub.end_date <= now + timedelta(days=d))
        period = f"{sub.end_date:%Y%m%d}"
        if not sub.auto_renew:
            key, task, kwargs = f"exp{t}:{period}", send_expiring_soon_email, {}
        elif not has_payment_method(sub.vendor_id, sub.end_date):
            key, task = f"pm{t}:{period}", send_payment_method_required_email
            kwargs = {'reason': "There's no working card or Mobile Money account to renew with.",
                      'no_method': True, 'access_until': lapse_date(sub, cfg).isoformat(), 'upcoming': True}
        else:
            if t != max(thresholds):
                continue  # one heads-up is enough when renewal is automatic
            key, task, kwargs = f"renew:{period}", send_expiring_soon_email, {}
        # Claim the reminder atomically so overlapping runs send it once.
        claimed = VendorSubscription.objects.filter(pk=sub.pk).exclude(last_reminder_key=key).update(
            last_reminder_key=key)
        if not claimed:
            continue
        if task is send_expiring_soon_email:
            task.delay(sub.vendor_id, sub.pk)
        else:
            task.delay(sub.vendor_id, **kwargs)
        sent += 1
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# 6. Refunds and chargebacks
# ─────────────────────────────────────────────────────────────────────────────

def handle_refund(reference, refunded_pesewas):
    """
    A subscription payment was refunded in full: the plan it paid for stops
    now. Partial refunds (goodwill credits) only get logged.
    """
    with transaction.atomic():
        txn = (PaymentTransaction.objects.select_for_update(of=('self',))
               .filter(paystack_reference=reference).first())
        if txn is None:
            return 'unknown'
        full = refunded_pesewas is None or refunded_pesewas + 1 >= pesewas(txn.amount)
        if not full:
            logger.info("Partial refund on %s (%s pesewas): plan unchanged", reference, refunded_pesewas)
            return 'partial'
        was = txn.status
        txn.status = 'refunded'
        txn.save(update_fields=['status'])
        if was != 'success' or not txn.subscription_id or txn.transaction_type not in PLAN_PURCHASE_TYPES + ('renewal',):
            return 'refunded'
        sub = VendorSubscription.objects.select_for_update(of=('self',)).select_related('vendor').get(pk=txn.subscription_id)
        if sub.status not in LIVE:
            return 'refunded'
        # Only revoke if this payment is what keeps the plan running now:
        # a refunded older renewal shouldn't end a later paid period.
        latest = (PaymentTransaction.objects.filter(subscription=sub, status='success')
                  .exclude(pk=txn.pk).filter(paid_at__gt=txn.paid_at or timezone.now()).exists())
        if latest:
            return 'refunded'
        sub.end_date = min(sub.end_date, timezone.now())
        sub.auto_renew = False
        sub.save(update_fields=['end_date', 'auto_renew', 'updated_at'])
        _expire(sub)
    logger.warning("Subscription %s ended: payment %s was refunded", txn.subscription_id, reference)
    return 'revoked'

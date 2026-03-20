# subscriptions/momo_services.py

import uuid
import logging
import requests
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.db import transaction as db_transaction
from django.core.cache import cache

from .models import (
    VendorSubscription, PaymentTransaction,
    SubscriptionPlan, SubscriptionUsage,
)
from .momo_models import BillingProfile, MomoAccount

logger = logging.getLogger(__name__)

PAYSTACK_BASE   = 'https://api.paystack.co'
PAYSTACK_SECRET = settings.PAYSTACK_SECRET_KEY

PROVIDER_PAYSTACK_MAP = {
    'mtn':        'mtn',
    'vodafone':   'vod',
    'airteltigo': 'tgo',
}

BILLING_DAYS = {
    'monthly':   30,
    'quarterly': 90,
    'yearly':    365,
}

CHARGE_CACHE_TTL = 900


def _headers():
    return {
        'Authorization': f'Bearer {PAYSTACK_SECRET}',
        'Content-Type':  'application/json',
    }


# ─────────────────────────────────────────────────────────────────────────────
# Billing Profile helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_billing_profile(vendor):
    profile, _ = BillingProfile.objects.get_or_create(
        vendor=vendor,
        defaults={
            'first_name': (getattr(vendor, 'name', '') or '').split()[0],
            'last_name':  ' '.join((getattr(vendor, 'name', '') or '').split()[1:]),
            'email':      getattr(getattr(vendor, 'user', None), 'email', '') or '',
        },
    )
    return profile


def require_billing_profile(vendor):
    profile = get_or_create_billing_profile(vendor)
    if not profile.is_complete:
        raise ValueError(
            'billing_profile_incomplete:Please complete your billing details before subscribing.'
        )
    return profile


# ─────────────────────────────────────────────────────────────────────────────
# Cache helpers
# ─────────────────────────────────────────────────────────────────────────────

def _inflight_key(vendor_id, plan_id):
    return f'momo_inflight:{vendor_id}:{plan_id}'

def _get_inflight(vendor_id, plan_id):
    return cache.get(_inflight_key(vendor_id, plan_id))

def _set_inflight(vendor_id, plan_id, reference):
    cache.set(_inflight_key(vendor_id, plan_id), reference, timeout=CHARGE_CACHE_TTL)

def _clear_inflight(vendor_id, plan_id):
    cache.delete(_inflight_key(vendor_id, plan_id))


# ─────────────────────────────────────────────────────────────────────────────
# Paystack helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_charge(reference):
    try:
        resp = requests.get(
            f'{PAYSTACK_BASE}/charge/{reference}',
            headers=_headers(), timeout=15,
        )
        data = resp.json()
        if data.get('status') not in (None, False, 'false', ''):
            return data.get('data') or {}
    except Exception as exc:
        logger.warning(f'_fetch_charge({reference}) failed: {exc}')
    return None


def _is_charge_alive(charge_data):
    if not charge_data:
        return False
    return charge_data.get('status') not in ('success', 'failed', 'abandoned', None)


def _is_charge_attempted(data):
    return 'charge attempted' in (data.get('message') or '').lower()


def _fresh_reference(vendor_id):
    return f'MOMO-{vendor_id}-{uuid.uuid4().hex[:8].upper()}'


def _mask(phone):
    p = phone.replace(' ', '')
    return ('•' * max(len(p) - 4, 0)) + p[-4:] if len(p) >= 4 else p


def _post_charge(reference, amount_pesewas, email, phone, ps_provider):
    """POST a MoMo charge to Paystack. Uses the actual phone number passed in."""
    payload = {
        'amount':    amount_pesewas,
        'email':     email,
        'currency':  'GHS',
        'reference': reference,
        'mobile_money': {
            'phone':    phone,        # ← uses the actual phone, not hardcoded
            'provider': ps_provider,
        },
    }
    resp = requests.post(
        f'{PAYSTACK_BASE}/charge', json=payload, headers=_headers(), timeout=30
    )
    return resp, resp.json()


def _get_most_recent_pending_momo(vendor):
    return PaymentTransaction.objects.filter(
        vendor=vendor,
        status='pending',
        paystack_reference__startswith='MOMO-',
    ).order_by('-created_at').first()


# ─────────────────────────────────────────────────────────────────────────────
# MoMo-specific subscription activation
# Called from poll_momo_status() when Paystack confirms success.
# Does NOT call Paystack again — we already know it succeeded.
# ─────────────────────────────────────────────────────────────────────────────

def _activate_momo_subscription(reference, intent):
    """
    Activate a subscription after MoMo payment confirmation.

    IDEMPOTENT: If this reference has already been processed (e.g. poll called
    twice before cache clears), returns the existing subscription immediately.

    RACE-SAFE: Uses select_for_update on the vendor row to ensure only one
    concurrent activation can proceed at a time.
    """
    from vendor.models import Vendor
    from django.db import IntegrityError

    vendor_id = intent['vendor_id']
    plan_id   = intent['plan_id']
    billing   = intent.get('billing', 'monthly')

    vendor = Vendor.objects.select_related('user').get(pk=vendor_id)
    plan   = SubscriptionPlan.objects.get(pk=plan_id)
    days   = BILLING_DAYS.get(billing, 30)

    with db_transaction.atomic():
        # ── Idempotency: if this reference is already activated, return early ──
        existing_txn = PaymentTransaction.objects.filter(
            paystack_reference=reference, status='success'
        ).select_related('subscription').first()
        if existing_txn and existing_txn.subscription and existing_txn.subscription.status == 'active':
            logger.info(f'MoMo ref={reference} already activated — returning existing sub')
            return existing_txn.subscription

        # ── Row-level lock on vendor to prevent concurrent activations ─────────
        # select_for_update blocks until any other transaction holding this lock
        # completes, so two simultaneous poll hits queue up rather than racing.
        Vendor.objects.select_for_update().get(pk=vendor_id)

        # ── Cancel all existing active/trial subscriptions ────────────────────
        cancelled = VendorSubscription.objects.filter(
            vendor=vendor, status__in=['active', 'trial']
        ).update(status='cancelled')
        if cancelled:
            logger.info(f'Cancelled {cancelled} existing subscription(s) for vendor {vendor_id}')

        # ── Create the new active subscription ────────────────────────────────
        try:
            sub = VendorSubscription.objects.create(
                vendor=vendor,
                plan=plan,
                status='active',
                start_date=timezone.now(),
                end_date=timezone.now() + timedelta(days=days),
                auto_renew=True,
                payment_reference=reference,
            )
        except IntegrityError:
            # Partial unique index fired — another concurrent activation won the race.
            # Find the subscription it created and return it.
            logger.warning(f'IntegrityError creating sub for vendor {vendor_id} ref={reference} — returning concurrent sub')
            sub = VendorSubscription.objects.filter(
                vendor=vendor, status='active'
            ).select_related('plan').first()
            if not sub:
                raise  # Unexpected — re-raise so the error surfaces
            return sub

        # ── Update/create usage tracker ───────────────────────────────────────
        usage, created = SubscriptionUsage.objects.get_or_create(
            vendor=vendor,
            defaults={
                'subscription':        sub,
                'active_products_count': 0,
                'period_start':        timezone.now(),
            },
        )
        if not created:
            usage.subscription = sub
            usage.reset_for_new_cycle()

        # ── Mark transaction as success ───────────────────────────────────────
        PaymentTransaction.objects.filter(
            paystack_reference=reference
        ).update(
            subscription=sub,
            status='success',
            paid_at=timezone.now(),
        )

        # ── Sync denormalised vendor flags ────────────────────────────────────
        vendor.is_subscribed            = True
        vendor.subscription_start_date  = sub.start_date.date()
        vendor.subscription_end_date    = sub.end_date.date()
        vendor.save(update_fields=[
            'is_subscribed', 'subscription_start_date', 'subscription_end_date'
        ])

    # Fire email outside the transaction — worker reads committed data
    try:
        from .tasks import send_subscription_confirmation_email
        send_subscription_confirmation_email.delay(vendor.id, sub.id)
    except Exception as exc:
        logger.warning(f'Failed to queue confirmation email for MoMo sub: {exc}')

    logger.info(
        f'MoMo subscription activated: vendor={vendor.id} plan={plan.name} '
        f'billing={billing} ref={reference}'
    )
    return sub


# ─────────────────────────────────────────────────────────────────────────────
# Initiate MoMo subscription charge
# ─────────────────────────────────────────────────────────────────────────────

def initiate_momo_charge(vendor, plan_id, billing, phone, provider, save=False):
    profile        = require_billing_profile(vendor)
    plan           = SubscriptionPlan.objects.get(pk=plan_id, is_active=True)
    ps_provider    = PROVIDER_PAYSTACK_MAP.get(provider, provider)
    amount_pesewas = int(float(plan.price) * 100)

    # Step 1: idempotency
    existing_ref = _get_inflight(vendor.id, plan_id)
    if existing_ref:
        charge = _fetch_charge(existing_ref)
        if _is_charge_alive(charge):
            logger.info(f'Reusing in-flight charge {existing_ref} for vendor {vendor.id}')
            return {
                'reference':    existing_ref,
                'status':       'pending',
                'display_text': charge.get('display_text') or 'Please approve on your phone.',
                'provider':     provider,
                'masked_phone': _mask(phone),
                'resumed':      True,
            }
        _clear_inflight(vendor.id, plan_id)

    # Step 2: fresh charge
    reference = _fresh_reference(vendor.id)
    logger.info(
        f'Initiating MoMo charge ref={reference} vendor={vendor.id} '
        f'plan={plan_id} provider={ps_provider} phone=...{phone[-4:]}'
    )
    resp, data = _post_charge(reference, amount_pesewas, profile.email, phone, ps_provider)

    # Step 3: handle "Charge attempted"
    if resp.status_code == 400 and _is_charge_attempted(data):
        inner        = data.get('data') or {}
        inner_status = inner.get('status', '')
        inner_msg    = inner.get('message', '')

        logger.warning(
            f'"Charge attempted" phone=...{phone[-4:]} inner_status={inner_status} '
            f'inner_msg={inner_msg} vendor={vendor.id}'
        )

        # Hard decline (e.g. test mode with real number)
        if inner_status == 'failed':
            PaymentTransaction.objects.filter(
                vendor=vendor,
                status='pending',
                paystack_reference=inner.get('reference', ''),
            ).update(status='failed', failure_reason=inner_msg)
            raise ValueError(f'Payment declined: {inner_msg}')

        # Active USSD session — try to resume
        pending_txn = _get_most_recent_pending_momo(vendor)
        if pending_txn:
            existing_charge = _fetch_charge(pending_txn.paystack_reference)
            if _is_charge_alive(existing_charge):
                logger.info(f'Resuming live charge {pending_txn.paystack_reference}')
                _set_inflight(vendor.id, plan_id, pending_txn.paystack_reference)
                return {
                    'reference':    pending_txn.paystack_reference,
                    'status':       'pending',
                    'display_text': (
                        existing_charge.get('display_text')
                        or 'A payment prompt was already sent to your phone. Please approve it.'
                    ),
                    'provider':     provider,
                    'masked_phone': _mask(phone),
                    'resumed':      True,
                }
            else:
                pending_txn.status         = 'failed'
                pending_txn.failure_reason = 'Session expired.'
                pending_txn.save(update_fields=['status', 'failure_reason'])

        raise ValueError(
            'pending_ussd_session:'
            'A payment prompt has been sent to your phone. '
            'Please check your phone and approve or decline the payment. '
            'If nothing appears, wait 2–3 minutes for the session to expire, then try again.'
        )

    # Step 4: other errors
    if resp.status_code >= 400 or not data.get('status'):
        msg = data.get('message') or 'Unknown Paystack error'
        logger.error(f'MoMo charge failed: {msg} ref={reference}')
        raise ValueError(f'Payment failed: {msg}')

    # Step 5: success — save intent to cache for activation on poll success
    charge       = data.get('data') or {}
    display_text = charge.get('display_text') or 'Please approve the payment on your phone.'

    _set_inflight(vendor.id, plan_id, reference)
    cache.set(f'momo_charge:{reference}', {
        'plan_id':   plan_id,
        'billing':   billing,
        'vendor_id': vendor.id,
        'phone':     phone,
        'provider':  provider,
        'save':      save,
    }, timeout=CHARGE_CACHE_TTL)

    active_sub = VendorSubscription.objects.filter(
        vendor=vendor, status__in=['active', 'trial']
    ).first()
    PaymentTransaction.objects.create(
        vendor=vendor, subscription=active_sub,
        transaction_type='initial',
        amount=plan.price, currency='GHS',
        status='pending', paystack_reference=reference,
    )

    if save:
        MomoAccount.objects.get_or_create(
            vendor=vendor, phone=phone,
            defaults={
                'provider':   provider,
                'is_default': not MomoAccount.objects.filter(vendor=vendor).exists(),
            },
        )

    # Pass the raw Paystack status through so the frontend can distinguish:
    # 'send_otp'    → MTN: show OTP input field (SMS code was sent)
    # 'pay_offline' → Vodafone/AirtelTigo: show "check phone" polling UI
    # 'pending'     → generic pending
    return {
        'reference':    reference,
        'status':       charge.get('status', 'pending'),
        'display_text': display_text,
        'provider':     provider,
        'masked_phone': _mask(phone),
        'requires_otp': charge.get('status') == 'send_otp',
        'resumed':      False,
    }




# ─────────────────────────────────────────────────────────────────────────────
# Submit OTP — for MTN MoMo which sends OTP via SMS
# ─────────────────────────────────────────────────────────────────────────────

def submit_momo_otp(reference, otp):
    """
    Submit the OTP received via SMS to Paystack.
    Called when Paystack returns status='send_otp' on the initial charge.
    
    After submission, the charge moves to 'pay_offline' (awaiting USSD approval)
    or directly to 'success'. Frontend should start polling after this.
    
    Returns: { status: 'pending'|'success'|'failed', message: str }
    """
    resp = requests.post(
        f'{PAYSTACK_BASE}/charge/submit_otp',
        json={'otp': otp, 'reference': reference},
        headers=_headers(),
        timeout=15,
    )
    data = resp.json()

    if not data.get('status'):
        raise ValueError(data.get('message') or 'OTP submission failed.')

    charge    = data.get('data') or {}
    ps_status = charge.get('status', 'pending')

    STATUS_MAP = {
        'success':     'success',
        'failed':      'failed',
        'abandoned':   'failed',
        'pay_offline': 'pending',
        'pending':     'pending',
        'processing':  'pending',
    }
    our_status = STATUS_MAP.get(ps_status, 'pending')

    logger.info(f'OTP submitted for ref={reference}: ps_status={ps_status} → {our_status}')
    return {
        'reference': reference,
        'status':    our_status,
        'message':   charge.get('display_text') or charge.get('message') or 'OTP accepted. Waiting for confirmation.',
    }

# ─────────────────────────────────────────────────────────────────────────────
# Poll MoMo charge status
# ─────────────────────────────────────────────────────────────────────────────

def poll_momo_status(reference):
    """
    Poll Paystack for the status of a MoMo charge.
    On success, activates the subscription using MoMo-specific logic
    (reads vendor/plan from our cache, NOT from Paystack metadata).
    """
    resp = requests.get(
        f'{PAYSTACK_BASE}/charge/{reference}', headers=_headers(), timeout=15
    )
    data = resp.json()

    if not data.get('status'):
        raise ValueError(f"Paystack error: {data.get('message', 'Unknown error')}")

    charge    = data.get('data') or {}
    ps_status = charge.get('status', 'pending')

    STATUS_MAP = {
        'success':     'success',
        'failed':      'failed',
        'abandoned':   'failed',
        'pending':     'pending',
        'send_otp':    'pending',
        'pay_offline': 'pending',
        'processing':  'pending',
    }
    our_status = STATUS_MAP.get(ps_status, 'pending')
    activated  = False

    if our_status == 'success':
        intent = cache.get(f'momo_charge:{reference}')
        if not intent:
            logger.error(
                f'poll_momo_status: no cache intent found for ref={reference}. '
                f'Cannot activate subscription — cache may have expired.'
            )
        else:
            try:
                _activate_momo_subscription(reference, intent)
                activated = True
                _clear_inflight(intent.get('vendor_id'), intent.get('plan_id'))
            except Exception as exc:
                logger.error(
                    f'MoMo activation error ref={reference}: {exc}', exc_info=True
                )

    elif our_status == 'failed':
        PaymentTransaction.objects.filter(
            paystack_reference=reference
        ).update(status='failed', failure_reason=charge.get('message', ''))
        intent = cache.get(f'momo_charge:{reference}')
        if intent:
            _clear_inflight(intent.get('vendor_id'), intent.get('plan_id'))

    return {
        'reference': reference,
        'status':    our_status,
        'message':   charge.get('display_text') or charge.get('message') or '',
        'activated': activated,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Manual MoMo payment — "Pay now" for overdue subscriptions
# ─────────────────────────────────────────────────────────────────────────────

def initiate_momo_manual_payment(vendor, momo_id=None, phone=None, provider=None):
    if momo_id:
        account  = MomoAccount.objects.get(pk=momo_id, vendor=vendor)
        phone    = account.phone
        provider = account.provider

    sub = VendorSubscription.objects.filter(
        vendor=vendor, status__in=['active', 'past_due']
    ).select_related('plan').first()
    if not sub:
        raise ValueError('No active subscription to renew.')

    profile     = require_billing_profile(vendor)
    reference   = f'MOMO-RNW-{vendor.id}-{uuid.uuid4().hex[:8].upper()}'
    ps_provider = PROVIDER_PAYSTACK_MAP.get(provider, provider)
    resp, data  = _post_charge(
        reference, int(float(sub.plan.price) * 100),
        profile.email, phone, ps_provider,
    )

    if resp.status_code == 400 and _is_charge_attempted(data):
        pending = PaymentTransaction.objects.filter(
            vendor=vendor, status='pending',
            paystack_reference__startswith='MOMO-RNW-',
        ).order_by('-created_at').first()
        if pending:
            ch = _fetch_charge(pending.paystack_reference)
            if _is_charge_alive(ch):
                return {
                    'reference':    pending.paystack_reference,
                    'status':       'pending',
                    'display_text': ch.get('display_text') or 'Please approve on your phone.',
                    'masked_phone': _mask(phone),
                    'provider':     provider,
                    'resumed':      True,
                }
        raise ValueError(
            'pending_ussd_session:'
            'A payment prompt is already active on this phone. '
            'Please check your phone or wait 2–3 minutes and try again.'
        )

    if resp.status_code >= 400 or not data.get('status'):
        raise ValueError(f"Payment failed: {data.get('message', 'Unknown error')}")

    charge       = data.get('data') or {}
    display_text = charge.get('display_text') or 'Please approve the payment on your phone.'

    # For renewals, store intent differently — no plan_id change
    cache.set(f'momo_charge:{reference}', {
        'vendor_id':    vendor.id,
        'plan_id':      sub.plan.id,
        'billing':      sub.billing_cycle if hasattr(sub, 'billing_cycle') else 'monthly',
        'sub_id':       sub.id,
        'type':         'renewal',
    }, timeout=CHARGE_CACHE_TTL)

    if momo_id:
        MomoAccount.objects.filter(pk=momo_id).update(last_reference=reference)

    return {
        'reference': reference, 'status': 'pending',
        'display_text': display_text,
        'masked_phone': _mask(phone), 'provider': provider, 'resumed': False,
    }
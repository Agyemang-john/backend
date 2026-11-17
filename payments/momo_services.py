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

def _activate_momo_subscription(reference, intent=None, paid_pesewas=None, currency='GHS', gateway_id=''):
    """
    Apply a MoMo payment that Paystack reports as successful. Everything is
    read from our PaymentTransaction row; the cache intent is only a fallback
    for charges started before plans were recorded on the transaction.
    Idempotent: a reference is applied once.
    """
    from . import subscription_lifecycle as lifecycle
    intent = intent or {}
    txn = PaymentTransaction.objects.filter(paystack_reference=reference).first()
    if txn is None:
        raise ValueError(f'Unknown MoMo reference {reference}')
    if paid_pesewas is None:
        paid_pesewas = lifecycle.pesewas(txn.amount)
    if txn.transaction_type == 'renewal':
        return lifecycle.apply_renewal_payment(txn.pk, paid_pesewas=paid_pesewas, currency=currency,
                                               gateway_id=gateway_id)
    return lifecycle.activate_paid_transaction(
        txn.pk, paid_pesewas=paid_pesewas, currency=currency, gateway_id=gateway_id,
        legacy_plan_id=intent.get('plan_id'), legacy_billing=intent.get('billing'),
    )


def _apply_momo_renewal(reference, intent=None, paid_pesewas=None, currency='GHS', gateway_id=''):
    """Kept for callers of the old name; see _activate_momo_subscription."""
    return _activate_momo_subscription(reference, intent, paid_pesewas, currency, gateway_id)


# ─────────────────────────────────────────────────────────────────────────────
# Initiate MoMo subscription charge
# ─────────────────────────────────────────────────────────────────────────────

def initiate_momo_charge(vendor, plan_id, billing, phone, provider, save=False):
    from .services import prepare_plan_change

    profile        = require_billing_profile(vendor)
    plan, quote, early = prepare_plan_change(vendor, plan_id, billing)
    if early:
        # Downgrade scheduled, free plan, or upgrade fully covered by credit.
        return {**early, 'status': 'scheduled' if early.get('scheduled') else 'activated'}
    plan_id        = plan.id
    ps_provider    = PROVIDER_PAYSTACK_MAP.get(provider, provider)
    from .subscription_lifecycle import pesewas
    amount_pesewas = pesewas(quote['amount_due'])

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
        'plan_id':    plan_id,
        'billing':    billing,
        'vendor_id':  vendor.id,
        'phone':      phone,
        'provider':   provider,
        'save':       save,
        'period_end': quote['period_end'].isoformat(),
    }, timeout=CHARGE_CACHE_TTL)

    active_sub = VendorSubscription.objects.filter(
        vendor=vendor, status__in=VendorSubscription.LIVE_STATUSES
    ).first()
    PaymentTransaction.objects.create(
        vendor=vendor, subscription=active_sub,
        transaction_type=quote['change_type'],
        amount=quote['amount_due'], currency='GHS',
        status='pending', paystack_reference=reference,
        plan=plan, billing=plan.billing_cycle,
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

def poll_momo_status(reference, vendor=None):
    """
    Poll Paystack for the status of a MoMo charge and apply it on success.
    `vendor` (the caller's store) must own the reference. What the payment
    buys comes from our PaymentTransaction, not from the cache.
    """
    txn = PaymentTransaction.objects.filter(paystack_reference=reference).first()
    if txn is None or (vendor is not None and txn.vendor_id != vendor.id):
        raise PermissionError('Unknown payment reference.')

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
    intent     = cache.get(f'momo_charge:{reference}') or {}

    if our_status == 'success':
        try:
            applied = _activate_momo_subscription(
                reference, intent,
                paid_pesewas=int(charge.get('amount') or 0),
                currency=charge.get('currency') or 'GHS',
                gateway_id=charge.get('id', ''),
            )
            activated = applied is not None
            _clear_inflight(txn.vendor_id, txn.plan_id or intent.get('plan_id'))
        except Exception as exc:
            logger.error(f'MoMo activation error ref={reference}: {exc}', exc_info=True)

    elif our_status == 'failed':
        PaymentTransaction.objects.filter(
            paystack_reference=reference, status='pending'
        ).update(status='failed', failure_reason=charge.get('message', ''))
        _clear_inflight(txn.vendor_id, txn.plan_id or intent.get('plan_id'))

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

    from .subscription_lifecycle import MANUAL_INFLIGHT_WINDOW, pesewas
    sub = VendorSubscription.objects.filter(
        vendor=vendor, status__in=['active', 'past_due']
    ).select_related('plan', 'scheduled_plan').first()
    if not sub or not sub.in_force():
        raise ValueError('Your plan has already ended. Choose a plan to subscribe again.')
    plan = sub.scheduled_plan or sub.plan
    if plan.price <= 0:
        raise ValueError('There is nothing to pay on this plan.')
    if PaymentTransaction.objects.filter(
            subscription=sub, transaction_type='renewal', status='pending',
            created_at__gte=timezone.now() - MANUAL_INFLIGHT_WINDOW).exists():
        raise ValueError('pending_ussd_session:A payment for this renewal is already in progress. '
                         'Approve it on your phone, or wait a few minutes and try again.')

    profile     = require_billing_profile(vendor)
    reference   = f'MOMO-RNW-{vendor.id}-{uuid.uuid4().hex[:8].upper()}'
    ps_provider = PROVIDER_PAYSTACK_MAP.get(provider, provider)
    resp, data  = _post_charge(
        reference, pesewas(plan.price),
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

    # The renewal is recorded in the database, tied to the period it pays for,
    # so it is applied exactly once whether the poll or the webhook sees it first.
    PaymentTransaction.objects.create(
        vendor=vendor, subscription=sub, transaction_type='renewal',
        amount=plan.price, currency='GHS', status='pending', paystack_reference=reference,
        plan=plan, billing=plan.billing_cycle, period_end=sub.end_date,
    )

    if momo_id:
        MomoAccount.objects.filter(pk=momo_id).update(last_reference=reference)

    return {
        'reference': reference, 'status': 'pending',
        'display_text': display_text,
        'masked_phone': _mask(phone), 'provider': provider, 'resumed': False,
    }
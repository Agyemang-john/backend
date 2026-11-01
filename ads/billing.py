"""
Paying for ads.

Spend is first taken from the store's earnings (one AD_SPEND ledger debit per
store per day, ads/services.bill_spend). When that leaves the balance below
zero and the store has a saved card (the one used for its subscription), the
shortfall is charged to the card and credited back to the ledger, so the store
never builds up ad debt. A declined card pauses the store's ads for
CARD_FAILURE_PAUSE and tells the seller.
"""
import logging
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

CARD_FAILURE_PAUSE = 3 * 86400
MIN_CHARGE = Decimal('1.00')      # Paystack minimum is GHS 0.10; keep card statements tidy


def saved_card(vendor_id):
    from payments.models import PaystackAuthorization
    return (PaystackAuthorization.objects.filter(vendor_id=vendor_id, is_reusable=True)
            .order_by('-is_default', '-id').first())


def card_paused(vendor_id):
    return bool(cache.get(f"ads:card_failed:{vendor_id}"))


def vendors_with_cards(vendor_ids):
    from payments.models import PaystackAuthorization
    return set(PaystackAuthorization.objects.filter(vendor_id__in=vendor_ids, is_reusable=True)
               .values_list('vendor_id', flat=True)) - {v for v in vendor_ids if card_paused(v)}


def balance(vendor_id):
    from payments.ledger_models import LedgerEntry
    return LedgerEntry.objects.filter(vendor_id=vendor_id, payout__isnull=True).aggregate(s=Sum('amount'))['s'] or Decimal('0')


def settle_with_card(vendor):
    """Charge the store's card for a negative balance. Returns the amount charged (0 if none/failed)."""
    shortfall = -balance(vendor.pk)
    if shortfall < MIN_CHARGE:
        return Decimal('0')
    card = saved_card(vendor.pk)
    if card is None:
        return Decimal('0')
    import requests

    from payments.ledger_models import LedgerEntry
    from payments.models import PaymentTransaction
    from payments.services import PAYSTACK_BASE, PAYSTACK_HEADERS, _generate_reference

    shortfall = shortfall.quantize(Decimal('0.01'))
    reference = _generate_reference(vendor, 'ADS')
    txn = PaymentTransaction.objects.create(vendor=vendor, authorization=card, transaction_type='ads',
                                            amount=shortfall, currency='GHS', status='pending',
                                            paystack_reference=reference)
    try:
        res = requests.post(f"{PAYSTACK_BASE}/transaction/charge_authorization", headers=PAYSTACK_HEADERS, timeout=20,
                            json={'authorization_code': card.authorization_code, 'email': vendor.email or vendor.user.email,
                                  'amount': int(shortfall * 100), 'reference': reference,
                                  'metadata': {'purpose': 'sponsored_products', 'vendor_id': vendor.pk}})
        data = res.json().get('data') or {}
    except Exception as exc:  # noqa: BLE001 - network
        data = {'status': 'failed', 'gateway_response': str(exc)[:200]}
    if data.get('status') != 'success':
        txn.status, txn.failure_reason = 'failed', data.get('gateway_response') or 'Declined'
        txn.save(update_fields=['status', 'failure_reason'])
        cache.set(f"ads:card_failed:{vendor.pk}", 1, CARD_FAILURE_PAUSE)
        _notify(vendor, f"We couldn't charge your card for sponsored products ({txn.failure_reason}). "
                        "Your ads are paused for 3 days or until you update your card.")
        logger.warning("ads card charge failed for vendor %s: %s", vendor.pk, txn.failure_reason)
        return Decimal('0')
    txn.status, txn.paid_at = 'success', timezone.now()
    txn.paystack_transaction_id = str(data.get('id', ''))
    txn.save(update_fields=['status', 'paid_at', 'paystack_transaction_id'])
    LedgerEntry.objects.create(vendor=vendor, entry_type=LedgerEntry.ADJUSTMENT, amount=shortfall,
                               description=f"Card payment for sponsored products ({card.card_type} ••{card.last4})",
                               available_at=timezone.now())
    return shortfall


def _notify(vendor, message):
    from notification.utils import send_notification
    if vendor.user_id:
        send_notification(vendor.user, 'announcement', target=vendor,
                          data={'title': 'Sponsored products', 'message': message, 'url': '/ads', 'audience': 'vendor'})

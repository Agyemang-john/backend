"""
What is audited, and how.

TRACKED maps 'app_label.Model' → the fields whose changes are recorded. Only
saves that change one of these fields write an entry, so routine saves (view
counters, timestamps) cost one extra primary-key read and nothing else.
Queryset .update()/.bulk_update() bypass model signals and aren't captured;
code paths that matter for money (ledger, payouts) also write their own rows.
"""
import contextvars
import json
from decimal import Decimal

from django.apps import apps
from django.db.models.signals import post_delete, post_save, pre_save

TRACKED = {
    'product.Product': ['title', 'price', 'old_price', 'status', 'total_quantity', 'vendor_id', 'review_note'],
    'product.Variants': ['price', 'quantity'],
    'product.Coupon': ['code', 'discount_amount', 'discount_percentage', 'valid_from', 'valid_to', 'active'],
    'product.FlashSale': ['sale_price', 'start_time', 'end_time', 'max_quantity', 'is_active'],
    'order.Order': ['status', 'total', 'payment_method', 'adminnote'],
    'order.OrderProduct': ['status', 'price', 'amount', 'quantity', 'refund_reason'],
    'order.Refund': ['amount', 'reason'],
    'payments.Payout': ['amount', 'status', 'transaction_id'],
    'payments.LedgerEntry': ['entry_type', 'amount', 'description', 'available_at', 'payout_id'],
    'payments.SubscriptionPlan': ['price', 'commission_rate', 'max_products', 'is_active'],
    'vendor.Vendor': ['status', 'is_approved', 'is_suspended', 'is_subscribed', 'subscription_end_date', 'shop_paused'],
    'vendor.VendorPaymentMethod': ['payment_method', 'momo_number', 'momo_provider', 'bank_name', 'bank_account_name',
                                   'bank_account_number'],
    'vendor.VendorMember': ['role', 'is_active'],
    'userauths.User': ['email', 'is_active', 'is_staff', 'is_superuser', 'is_suspended'],
    'protection.Claim': ['status', 'resolution_amount', 'refund_sent_at'],
    'risk.OrderRisk': ['status', 'note'],
    'tax.TaxRate': ['rate', 'active', 'effective_from'],
    'tax.SellerTaxRegistration': ['vat_registered', 'tax_id', 'verified'],
    'verification.VerificationProfile': ['id_number', 'business_reg_number', 'documents', 'needs_recheck'],
    'ads.AdCampaign': ['status', 'bid', 'daily_budget'],
}
# Values never written to the log (they'd turn it into a copy of secrets).
MASKED = {'momo_number', 'bank_account_number', 'id_number'}

_old_values = contextvars.ContextVar('audit_old_values', default=None)


def _plain(value, model=None, field=None):
    if isinstance(value, (Decimal, int, float)) and model is not None and field is not None:
        try:
            dp = model._meta.get_field(field).decimal_places
        except Exception:  # noqa: BLE001 - not a DecimalField
            dp = None
        if dp is not None:
            return f"{Decimal(str(value)):.{dp}f}"     # 10 and 10.00 are the same price
    if isinstance(value, Decimal):
        return str(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _mask(field, value):
    if field in MASKED and value:
        s = str(value)
        return '•' * max(0, len(s) - 3) + s[-3:]
    return value


def _snapshot(instance, fields):
    return {f: _plain(getattr(instance, f, None), type(instance), f) for f in fields}


def _pre_save(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        return
    fields = TRACKED[sender._meta.label]
    old = sender._default_manager.filter(pk=instance.pk).values(*fields).first()
    store = _old_values.get() or {}
    store[(sender._meta.label, instance.pk)] = {f: _plain(v, sender, f) for f, v in (old or {}).items()}
    _old_values.set(store)


def _post_save(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    from .services import write
    label = sender._meta.label
    fields = TRACKED[label]
    new = _snapshot(instance, fields)
    if created:
        changes = {f: [None, _mask(f, v)] for f, v in new.items() if v not in (None, '', [], {})}
        write('create', instance, changes)
        return
    store = _old_values.get() or {}
    old = store.pop((label, instance.pk), None)
    if old is None:
        return
    changes = {f: [_mask(f, old.get(f)), _mask(f, new[f])] for f in fields if old.get(f) != new[f]}
    if changes:
        write('update', instance, changes)


def _post_delete(sender, instance, **kwargs):
    from .services import write
    write('delete', instance, {f: [_mask(f, v), None] for f, v in _snapshot(instance, TRACKED[sender._meta.label]).items()})


def connect():
    import logging
    from django.core.exceptions import FieldDoesNotExist
    for label, wanted in list(TRACKED.items()):
        try:
            model = apps.get_model(label)
        except LookupError:
            TRACKED.pop(label)
            continue
        # A misspelt field would break every save of the model: drop it loudly instead.
        valid = []
        for f in wanted:
            names = [f] + ([f[:-3]] if f.endswith('_id') else [])   # 'vendor_id' is the FK 'vendor'
            for name in names:
                try:
                    model._meta.get_field(name)
                    valid.append(f)
                    break
                except FieldDoesNotExist:
                    continue
            else:
                logging.getLogger(__name__).warning("audit: %s has no field %s", label, f)
        TRACKED[label] = valid
        uid = f"audit:{label}"
        pre_save.connect(_pre_save, sender=model, dispatch_uid=f"{uid}:pre", weak=False)
        post_save.connect(_post_save, sender=model, dispatch_uid=f"{uid}:post", weak=False)
        post_delete.connect(_post_delete, sender=model, dispatch_uid=f"{uid}:del", weak=False)

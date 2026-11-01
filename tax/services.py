"""
Working out the tax inside tax-inclusive amounts.

    breakdown_lines(lines) -> per line: tax components + exclusive value
Every call is set-based: rates, exemptions and registrations are each fetched
once per call (rates/exemptions also cached), whatever the number of lines.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.core.cache import cache


from .models import SellerTaxRegistration, TaxExemption, TaxRate

CENT = Decimal('0.01')
CACHE_SECONDS = 600


def _q(v):
    return Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


def rates_for(country, on_date=None):
    """Active components for a country, effective on `on_date` (latest per code)."""
    key = f"tax_rates:{country}"
    rates = cache.get(key)
    if rates is None:
        rates = list(TaxRate.objects.filter(country=country, active=True).order_by('position', 'code', '-effective_from'))
        cache.set(key, rates, CACHE_SECONDS)
    from datetime import date
    on_date = on_date or date.today()
    chosen = {}
    for r in rates:
        if r.effective_from and r.effective_from > on_date:
            continue   # not started yet
        current = chosen.get(r.code)
        # Of the rates that have started, the most recent one wins (blank = oldest).
        if current is None or (r.effective_from or date.min) > (current.effective_from or date.min):
            chosen[r.code] = r
    return sorted(chosen.values(), key=lambda r: (r.position, r.code))


def exempt_category_ids(country):
    key = f"tax_exempt:{country}"
    hit = cache.get(key)
    if hit is None:
        cats, subs = set(), set()
        for c, s in TaxExemption.objects.filter(country=country).values_list('category_id', 'sub_category_id'):
            if c:
                cats.add(c)
            if s:
                subs.add(s)
        hit = (cats, subs)
        cache.set(key, hit, CACHE_SECONDS)
    return hit


def clear_cache():
    countries = set(TaxRate.objects.values_list('country', flat=True)) | set(
        TaxExemption.objects.values_list('country', flat=True))
    cache.delete_many([f"tax_rates:{c}" for c in countries] + [f"tax_exempt:{c}" for c in countries])


def registrations(vendor_ids):
    return {r.vendor_id: r for r in SellerTaxRegistration.objects.filter(vendor_id__in=vendor_ids, vat_registered=True)}


@dataclass
class LineTax:
    amount: Decimal                  # inclusive, what the shopper paid
    exclusive: Decimal
    tax: Decimal
    components: list = field(default_factory=list)   # [{'code','name','rate','amount'}]
    note: str = ''                   # why there's no tax, if none


def split_inclusive(amount, rates):
    """Tax inside an inclusive amount, split by component; rounding remainder goes to the first component."""
    amount = Decimal(amount)
    total_rate = sum((r.rate for r in rates), Decimal('0'))
    if total_rate <= 0 or amount <= 0:
        return LineTax(amount=_q(amount), exclusive=_q(amount), tax=Decimal('0.00'))
    tax = _q(amount * total_rate / (Decimal('100') + total_rate))
    parts = [{'code': r.code, 'name': r.name, 'rate': r.rate,
              'amount': _q(tax * r.rate / total_rate)} for r in rates]
    drift = tax - sum(p['amount'] for p in parts)
    if parts and drift:
        parts[0]['amount'] += drift
    return LineTax(amount=_q(amount), exclusive=_q(amount) - tax, tax=tax, components=parts)


def breakdown_lines(lines, on_date=None):
    """
    `lines`: iterable of objects/dicts with product (with vendor, sub_category) and amount.
    Returns [LineTax] in the same order.
    """
    lines = list(lines)
    vendor_ids = {getattr(_get(l, 'product'), 'vendor_id', None) for l in lines} - {None}
    regs = registrations(vendor_ids)
    out = []
    for l in lines:
        product = _get(l, 'product')
        amount = Decimal(_get(l, 'amount') or 0)
        vendor = getattr(product, 'vendor', None) if product else None
        if vendor is None or vendor.pk not in regs:
            out.append(LineTax(amount=_q(amount), exclusive=_q(amount), tax=Decimal('0.00'),
                               note='Seller not VAT registered'))
            continue
        country = str(vendor.country or 'GH')
        cats, subs = exempt_category_ids(country)
        sub = getattr(product, 'sub_category', None)
        if sub is not None and (sub.pk in subs or sub.category_id in cats):
            out.append(LineTax(amount=_q(amount), exclusive=_q(amount), tax=Decimal('0.00'), note='VAT exempt'))
            continue
        out.append(split_inclusive(amount, rates_for(country, on_date)))
    return out


def summarise(line_taxes):
    """Totals by component across lines: [{'code','name','rate','amount'}], total tax."""
    by_code = {}
    for lt in line_taxes:
        for c in lt.components:
            entry = by_code.setdefault(c['code'], {**c, 'amount': Decimal('0.00')})
            entry['amount'] += c['amount']
    total = sum((lt.tax for lt in line_taxes), Decimal('0.00'))
    return list(by_code.values()), total


def _get(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

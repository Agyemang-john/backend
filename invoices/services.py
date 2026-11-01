"""Issuing invoices: snapshot the order once, number it from a sequence."""
from collections import OrderedDict
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, connection, transaction

from tax.services import breakdown_lines, registrations, summarise

from .models import Invoice

PAID_STATUSES_EXCLUDED = ('canceled',)


def next_number(issued_at):
    with connection.cursor() as cur:
        cur.execute("SELECT nextval('invoice_number_seq')")
        seq = cur.fetchone()[0]
    prefix = getattr(settings, 'INVOICE_PREFIX', 'NM')
    return f"{prefix}{issued_at:%y}-{seq:07d}"


def _money(v):
    return str(Decimal(v).quantize(Decimal('0.01')))


def _option(line):
    v = line.variant
    if v is None:
        return ''
    return ' / '.join(n for n in (getattr(v.size, 'name', ''), getattr(v.color, 'name', '')) if n)


def _company():
    return {
        'name': getattr(settings, 'INVOICE_COMPANY_NAME', 'Negromart'),
        'address': getattr(settings, 'INVOICE_COMPANY_ADDRESS', 'Accra, Ghana'),
        'email': getattr(settings, 'INVOICE_COMPANY_EMAIL', 'support@negromart.com'),
        'tax_id': getattr(settings, 'INVOICE_COMPANY_TAX_ID', ''),
    }


def build_snapshot(order, vendor=None):
    """Plain-JSON description of the order (or one store's part of it)."""
    lines = list(order.order_products.select_related(
        'product__vendor', 'product__sub_category', 'variant__size', 'variant__color').order_by('pk'))
    if vendor is not None:
        lines = [l for l in lines if l.product and l.product.vendor_id == vendor.pk]
    taxes = breakdown_lines(lines, on_date=order.date_created.date())
    regs = registrations({l.product.vendor_id for l in lines if l.product and l.product.vendor_id})

    sellers = OrderedDict()
    rows = []
    for line, lt in zip(lines, taxes):
        product = line.product
        seller = product.vendor if product else None
        if seller is not None and seller.pk not in sellers:
            reg = regs.get(seller.pk)
            sellers[seller.pk] = {
                'name': (reg.registered_name if reg and reg.registered_name else seller.name),
                'tax_id': reg.tax_id if reg else '', 'vat_registered': reg is not None,
            }
        rows.append({
            'title': product.title if product else 'Item no longer listed',
            'option': _option(line),
            'sku': getattr(product, 'sku', '') or '',
            'seller_id': seller.pk if seller else None,
            'quantity': line.quantity,
            'unit_price': _money(line.price),
            'amount': _money(line.amount),
            'tax': _money(lt.tax),
            'exclusive': _money(lt.exclusive),
            'tax_note': lt.note,
            'status': line.status,
        })
    components, tax_total = summarise(taxes)
    subtotal = sum((Decimal(r['amount']) for r in rows), Decimal('0'))
    if vendor is None:
        total = Decimal(order.total)
        charges = max(total - subtotal, Decimal('0'))   # delivery + packaging, as paid
    else:
        charges = Decimal('0')
        total = subtotal

    a = order.address
    return {
        'company': _company(),
        'order_number': order.order_number,
        'order_date': order.date_created.isoformat(),
        'payment_method': order.get_payment_method_display(),
        'buyer': {
            'name': (getattr(a, 'full_name', '') or (f"{order.user.first_name} {order.user.last_name}" if order.user else '') or '').strip(),
            'email': getattr(a, 'email', '') or (order.user.email if order.user else ''),
            'phone': getattr(a, 'mobile', '') or '',
            'address': ', '.join(p for p in (getattr(a, 'address', ''), getattr(a, 'town', ''),
                                             getattr(a, 'region', ''), getattr(a, 'country', '')) if p),
        },
        'sellers': {str(k): v for k, v in sellers.items()},
        'lines': rows,
        'subtotal': _money(subtotal),
        'charges': _money(charges),
        'total': _money(total),
        'tax_total': _money(tax_total),
        'tax_components': [{'name': c['name'], 'amount': _money(c['amount'])} for c in components],
        'prices_include_tax': True,
    }


def issue(order, vendor=None):
    """Get or create the receipt (vendor=None) or one store's invoice. Safe to call repeatedly/concurrently."""
    kind = Invoice.SELLER if vendor is not None else Invoice.RECEIPT
    existing = Invoice.objects.filter(order=order, kind=kind, vendor=vendor).first()
    if existing:
        return existing
    snapshot = build_snapshot(order, vendor)
    if not snapshot['lines']:
        return None   # lines not written yet, or none for this store
    from django.utils import timezone
    now = timezone.now()
    try:
        with transaction.atomic():
            return Invoice.objects.create(
                number=next_number(now), kind=kind, order=order, vendor=vendor,
                total=Decimal(snapshot['total']), tax_total=Decimal(snapshot['tax_total']), data=snapshot,
            )
    except IntegrityError:   # issued by a concurrent request
        return Invoice.objects.get(order=order, kind=kind, vendor=vendor)


def issue_all(order):
    """Receipt + one invoice per store; called when an order is placed."""
    issue(order)
    for vendor in {l.product.vendor for l in order.order_products.select_related('product__vendor')
                   if l.product and l.product.vendor}:
        issue(order, vendor)

"""
tools/labels.py
Printable shipping documents for many orders at once (PDF, reportlab):

  labels         ship-to block, order number + Code 128 barcode, item count and
                 weight, the store as sender. 4x6" (thermal printers) or 4 per A4.
  packing_slips  one A4 page per order: what's in the parcel, with tick boxes.
  pick_list      everything to pull off the shelves for the chosen orders,
                 grouped by product and version, with the orders it goes to.

Only the store's own lines of each order are included (an order can hold items
from several stores). Fonts: reportlab's bundled Vera, which covers more
characters than the built-in Helvetica.
"""
import io
import os
from collections import OrderedDict, defaultdict

from django.utils import timezone
from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from order.models import Order, OrderProduct
from product.variations import option_label

MAX_ORDERS = 100
LABEL_4X6 = (4 * inch, 6 * inch)


def _fonts():
    import reportlab
    folder = os.path.join(os.path.dirname(reportlab.__file__), 'fonts')
    try:
        pdfmetrics.getFont('Vera')
    except KeyError:
        pdfmetrics.registerFont(TTFont('Vera', os.path.join(folder, 'Vera.ttf')))
        pdfmetrics.registerFont(TTFont('VeraBd', os.path.join(folder, 'VeraBd.ttf')))
    return 'Vera', 'VeraBd'


def orders_for(vendor, order_ids):
    """The store's orders among these ids, with only its own lines (in order)."""
    orders = list(Order.objects.filter(pk__in=order_ids, vendors=vendor, is_ordered=True)
                  .select_related('address', 'address__pickup_point', 'user').order_by('date_created')[:MAX_ORDERS])
    lines = defaultdict(list)
    for line in (OrderProduct.objects.filter(order__in=orders, product__vendor=vendor).exclude(status='canceled')
                 .select_related('product', 'variant', 'variant__size', 'variant__color').order_by('pk')):
        lines[line.order_id].append(line)
    return [(o, lines[o.pk]) for o in orders if lines[o.pk]]


def number(order):
    """The order number printed and barcoded (the id if an old order has none)."""
    return order.order_number or str(order.pk)


def _ship_to(order):
    a = order.address
    if a is None:
        name = f'{getattr(order.user, "first_name", "")} {getattr(order.user, "last_name", "")}'.strip()
        return {'name': name or 'Customer', 'lines': ['No address on the order'], 'phone': ''}
    out = []
    if a.pickup_point_id:
        p = a.pickup_point
        out += [f'PICKUP: {p.name}', p.address, p.town]
    else:
        out += [a.address or '', a.address_line2, a.landmark and f'Near {a.landmark}',
                ', '.join(x for x in [a.town, a.region] if x), a.gps_address and f'GPS {a.gps_address}']
    out.append(a.country or '')
    return {'name': a.full_name or '', 'lines': [x for x in out if x], 'phone': a.mobile or ''}


def _wrap(c, text, font, size, width):
    words, line, out = (text or '').split(), '', []
    for w in words:
        trial = f'{line} {w}'.strip()
        if c.stringWidth(trial, font, size) <= width:
            line = trial
        else:
            if line:
                out.append(line)
            line = w
    if line:
        out.append(line)
    return out


def _label(c, x, y, w, h, vendor, order, lines):
    regular, bold = _fonts()
    pad = 6 * mm
    c.setLineWidth(1)
    c.rect(x + 2, y + 2, w - 4, h - 4)
    top = y + h - pad
    c.setFont(bold, 9)
    c.drawString(x + pad, top - 4, 'FROM')
    c.setFont(regular, 9)
    c.drawString(x + pad + 40, top - 4, vendor.name[:45])
    c.line(x + pad, top - 12, x + w - pad, top - 12)

    ship = _ship_to(order)
    c.setFont(bold, 9)
    c.drawString(x + pad, top - 28, 'SHIP TO')
    c.setFont(bold, 15)
    cursor = top - 46
    for part in _wrap(c, ship['name'], bold, 15, w - 2 * pad)[:2]:
        c.drawString(x + pad, cursor, part)
        cursor -= 18
    c.setFont(regular, 11)
    for raw in ship['lines']:
        for part in _wrap(c, raw, regular, 11, w - 2 * pad)[:2]:
            if cursor < y + h * 0.42:
                break
            c.drawString(x + pad, cursor, part)
            cursor -= 14
    if ship['phone']:
        c.setFont(bold, 12)
        c.drawString(x + pad, cursor - 4, f'Tel {ship["phone"]}')

    base = y + pad
    barcode = code128.Code128(number(order)[:40], barHeight=16 * mm, barWidth=0.9)
    bw = min(barcode.width, w - 2 * pad)
    barcode.drawOn(c, x + (w - bw) / 2, base + 22)
    c.setFont(bold, 11)
    c.drawCentredString(x + w / 2, base + 8, f'Order {number(order)}')
    units = sum(line.quantity for line in lines)
    weight = sum((line.product.weight or 0) * line.quantity for line in lines if line.product)
    c.setFont(regular, 9)
    c.drawString(x + pad, base + 22 + 16 * mm + 8,
                 f'{units} item{"s" if units != 1 else ""} · about {weight:.1f} kg · {order.date_created:%d %b %Y}')


def labels_pdf(vendor, orders, size='4x6'):
    buf = io.BytesIO()
    if size == 'a4':
        c = canvas.Canvas(buf, pagesize=A4)
        pw, ph = A4
        w, h = pw / 2, ph / 2
        slots = [(0, h), (w, h), (0, 0), (w, 0)]
        for i, (order, lines) in enumerate(orders):
            if i and i % 4 == 0:
                c.showPage()
            sx, sy = slots[i % 4]
            _label(c, sx, sy, w, h, vendor, order, lines)
    else:
        c = canvas.Canvas(buf, pagesize=LABEL_4X6)
        for i, (order, lines) in enumerate(orders):
            if i:
                c.showPage()
            _label(c, 0, 0, *LABEL_4X6, vendor, order, lines)
    c.save()
    return buf.getvalue()


def packing_slips_pdf(vendor, orders):
    regular, bold = _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    pw, ph = A4
    m = 18 * mm
    for i, (order, lines) in enumerate(orders):
        if i:
            c.showPage()
        y = ph - m
        c.setFont(bold, 18)
        c.drawString(m, y, 'Packing slip')
        c.setFont(regular, 10)
        c.drawRightString(pw - m, y, vendor.name[:60])
        y -= 22
        c.drawString(m, y, f'Order {number(order)}  ·  {order.date_created:%d %b %Y}')
        y -= 26
        ship = _ship_to(order)
        c.setFont(bold, 11)
        c.drawString(m, y, f'Ship to: {ship["name"]}')
        c.setFont(regular, 10)
        for raw in ship['lines'] + ([f'Tel {ship["phone"]}'] if ship['phone'] else []):
            y -= 14
            c.drawString(m + 10, y, raw[:95])
        y -= 28
        c.setFont(bold, 10)
        c.drawString(m, y, 'Packed')
        c.drawString(m + 50, y, 'Item')
        c.drawRightString(pw - m - 60, y, 'SKU')
        c.drawRightString(pw - m, y, 'Qty')
        y -= 6
        c.line(m, y, pw - m, y)
        c.setFont(regular, 10)
        for line in lines:
            title = line.product.title if line.product else 'Deleted product'
            version = option_label(line.variant) if line.variant_id else ''
            sku = (line.variant.seller_sku if line.variant_id else '') or (line.product.seller_sku if line.product else '')
            rows = _wrap(c, f'{title}{" (" + version + ")" if version else ""}', regular, 10, pw - 2 * m - 180)
            y -= 18
            if y < m + 40:
                c.showPage()
                c.setFont(regular, 10)
                y = ph - m
            c.rect(m + 8, y - 2, 10, 10)
            c.drawString(m + 50, y, rows[0] if rows else '')
            c.drawRightString(pw - m - 60, y, sku[:20])
            c.drawRightString(pw - m, y, str(line.quantity))
            for extra in rows[1:3]:
                y -= 13
                c.drawString(m + 50, y, extra)
        y -= 30
        c.setFont(regular, 9)
        c.drawString(m, max(y, m), f'Printed {timezone.localtime():%d %b %Y %H:%M}. Thank you for shopping with {vendor.name}.')
    c.save()
    return buf.getvalue()


def pick_rows(orders):
    """[(title, version, sku, quantity, [order numbers])] sorted by title."""
    grouped = OrderedDict()
    for order, lines in orders:
        for line in lines:
            key = (line.product_id, line.variant_id)
            if key not in grouped:
                title = line.product.title if line.product else 'Deleted product'
                version = option_label(line.variant) if line.variant_id else ''
                sku = (line.variant.seller_sku if line.variant_id else '') or (line.product.seller_sku if line.product else '')
                grouped[key] = [title, version, sku, 0, []]
            grouped[key][3] += line.quantity
            grouped[key][4].append(number(order))
    return sorted((tuple(v) for v in grouped.values()), key=lambda r: (r[0].lower(), r[1]))


def pick_list_pdf(vendor, orders):
    regular, bold = _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    pw, ph = A4
    m = 15 * mm
    y = ph - m
    c.setFont(bold, 18)
    c.drawString(m, y, 'Pick list')
    c.setFont(regular, 10)
    c.drawRightString(pw - m, y, f'{vendor.name[:50]} · {len(orders)} order{"s" if len(orders) != 1 else ""} · {timezone.localtime():%d %b %Y %H:%M}')
    y -= 26

    def header(y):
        c.setFont(bold, 10)
        c.drawString(m, y, 'Picked')
        c.drawString(m + 45, y, 'Product')
        c.drawString(m + 300, y, 'SKU')
        c.drawRightString(m + 400, y, 'Qty')
        c.drawString(m + 410, y, 'Orders')
        c.line(m, y - 5, pw - m, y - 5)
        return y - 20

    y = header(y)
    c.setFont(regular, 10)
    for title, version, sku, qty, numbers in pick_rows(orders):
        rows = _wrap(c, f'{title}{" (" + version + ")" if version else ""}', regular, 10, 245)
        if y < m + 30:
            c.showPage()
            y = header(ph - m)
            c.setFont(regular, 10)
        c.rect(m + 6, y - 2, 10, 10)
        c.drawString(m + 45, y, rows[0] if rows else '')
        c.drawString(m + 300, y, sku[:14])
        c.setFont(bold, 11)
        c.drawRightString(m + 400, y, str(qty))
        c.setFont(regular, 8)
        c.drawString(m + 410, y, ', '.join(numbers)[:60])
        c.setFont(regular, 10)
        for extra in rows[1:3]:
            y -= 13
            c.drawString(m + 45, y, extra)
        y -= 18
    c.save()
    return buf.getvalue()

"""Render an Invoice snapshot to PDF bytes (reportlab, no external binaries)."""
from io import BytesIO

from django.utils.dateparse import parse_datetime
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Invoice

GREY = colors.HexColor('#666666')
LINE = colors.HexColor('#DDDDDD')


def _styles():
    base = getSampleStyleSheet()
    return {
        'title': ParagraphStyle('t', parent=base['Title'], fontSize=18, alignment=0, spaceAfter=2),
        'h': ParagraphStyle('h', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=10, spaceAfter=2),
        'n': ParagraphStyle('n', parent=base['Normal'], fontSize=9, leading=12),
        'small': ParagraphStyle('s', parent=base['Normal'], fontSize=8, leading=10, textColor=GREY),
        'r': ParagraphStyle('r', parent=base['Normal'], fontSize=9, leading=12, alignment=TA_RIGHT),
        'rb': ParagraphStyle('rb', parent=base['Normal'], fontName='Helvetica-Bold', fontSize=10, alignment=TA_RIGHT),
    }


def _esc(text):
    return (str(text or '')).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def render(invoice: Invoice) -> bytes:
    d = invoice.data
    st = _styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title=f"{invoice.get_kind_display()} {invoice.number}",
                            author=d['company']['name'])
    cur = invoice.currency
    story = []

    title = 'Receipt' if invoice.kind == Invoice.RECEIPT else 'Invoice'
    if invoice.kind == Invoice.RECEIPT or not any(s.get('vat_registered') for s in d['sellers'].values()):
        heading = title
    else:
        heading = 'Tax invoice'
    company = d['company']
    left = [Paragraph(_esc(company['name']), st['title']),
            Paragraph(_esc(company['address']), st['small']),
            Paragraph(_esc(company['email']), st['small'])]
    if company.get('tax_id'):
        left.append(Paragraph(f"TIN {_esc(company['tax_id'])}", st['small']))
    order_date = parse_datetime(d['order_date'])
    right = [Paragraph(f"<b>{heading}</b>", st['rb']),
             Paragraph(f"No. {_esc(invoice.number)}", st['r']),
             Paragraph(f"Issued {invoice.issued_at:%d %b %Y}", st['r']),
             Paragraph(f"Order {_esc(d['order_number'])}", st['r']),
             Paragraph(f"Ordered {order_date:%d %b %Y}" if order_date else '', st['r']),
             Paragraph(f"Paid by {_esc(d['payment_method'])}", st['r'])]
    head = Table([[left, right]], colWidths=[100 * mm, 78 * mm])
    head.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story += [head, Spacer(1, 6 * mm)]

    buyer = d['buyer']
    bill = [Paragraph('Bill to / Deliver to', st['h']), Paragraph(_esc(buyer['name']), st['n'])]
    for k in ('address', 'phone', 'email'):
        if buyer.get(k):
            bill.append(Paragraph(_esc(buyer[k]), st['n']))
    sellers_block = [Paragraph('Sold by' if len(d['sellers']) == 1 else 'Sellers', st['h'])]
    for s in d['sellers'].values():
        tail = f" · TIN {_esc(s['tax_id'])}" if s.get('tax_id') else ''
        sellers_block.append(Paragraph(f"{_esc(s['name'])}{tail}", st['n']))
    parties = Table([[bill, sellers_block]], colWidths=[89 * mm, 89 * mm])
    parties.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story += [parties, Spacer(1, 6 * mm)]

    show_seller = len(d['sellers']) > 1
    rows = [['Item', 'Qty', f'Unit ({cur})', f'Tax ({cur})', f'Amount ({cur})']]
    for line in d['lines']:
        desc = f"<b>{_esc(line['title'])}</b>"
        if line['option']:
            desc += f"<br/><font size=8 color='#666666'>{_esc(line['option'])}</font>"
        if show_seller and line['seller_id'] is not None:
            seller = d['sellers'].get(str(line['seller_id']), {})
            desc += f"<br/><font size=8 color='#666666'>Sold by {_esc(seller.get('name'))}</font>"
        if line['status'] == 'canceled':
            desc += "<br/><font size=8 color='#C62828'>Cancelled</font>"
        rows.append([Paragraph(desc, st['n']), str(line['quantity']), line['unit_price'],
                     line['tax'] if line['tax'] != '0.00' else '—', line['amount']])
    table = Table(rows, colWidths=[90 * mm, 14 * mm, 24 * mm, 24 * mm, 26 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, colors.black),
        ('LINEBELOW', (0, 1), (-1, -1), 0.3, LINE),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story += [table, Spacer(1, 4 * mm)]

    totals = [['Items subtotal', d['subtotal']]]
    if d['charges'] != '0.00':
        totals.append(['Delivery & handling', d['charges']])
    totals.append([f'Total ({cur})', d['total']])
    for c in d['tax_components']:
        totals.append([f"Includes {c['name']}", c['amount']])
    tt = Table(totals, colWidths=[50 * mm, 30 * mm], hAlign='RIGHT')
    total_row = 2 if d['charges'] != '0.00' else 1
    tt.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, total_row), (-1, total_row), 'Helvetica-Bold'),
        ('LINEABOVE', (0, total_row), (-1, total_row), 0.8, colors.black),
        ('TEXTCOLOR', (0, total_row + 1), (-1, -1), GREY),
    ]))
    story += [tt, Spacer(1, 8 * mm)]

    notes = ['Prices include taxes where the seller is VAT registered.']
    if invoice.kind == Invoice.RECEIPT:
        notes.append(f"Sold through {company['name']} on behalf of the sellers listed above. "
                     "Returns and refunds follow each item's return policy.")
    story += [Paragraph(_esc(n), st['small']) for n in notes]
    doc.build(story)
    return buf.getvalue()

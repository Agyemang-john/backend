"""
core/admin_dashboard.py

Numbers for the Django admin home page (templates/admin/index.html).

Everything is computed in a handful of aggregate queries and cached for a few
minutes, so the dashboard costs nothing on most page loads however big the
tables get. Sections are filtered per user: a staff member only sees the
figures and queues for models they have view permission on.
"""

from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

CACHE_KEY = 'admin_dashboard:v1'
CACHE_SECONDS = 300

#: Orders that count as sales: paid/confirmed and not cancelled.
SALE_STATUSES_EXCLUDED = ('canceled',)

LOW_STOCK_THRESHOLD = 3

#: AdminLTE badge colour per order status.
STATUS_BADGE = {
    'pending': 'warning', 'processing': 'info', 'shipped': 'primary',
    'partially_delivered': 'primary', 'delivered': 'success', 'canceled': 'secondary',
}


def _changelist(model_label, **filters):
    app_label, model_name = model_label.split('.')
    url = reverse(f'admin:{app_label}_{model_name.lower()}_changelist')
    if filters:
        url += '?' + urlencode(filters)
    return url


def _pct_change(current, previous):
    if not previous:
        return None
    return round((float(current or 0) - float(previous)) / float(previous) * 100, 1)


def _compute():
    from notification.models import ContactInquiry
    from order.models import Order, OrderProduct, ReturnRequest
    from payments.models import Payout
    from product.models import Product, ProductReview, ReviewReport, Variants
    from userauths.models import User
    from vendor.models import Vendor

    now = timezone.now()
    today_start = timezone.make_aware(datetime.combine(timezone.localdate(), time.min))
    d30, d60 = now - timedelta(days=30), now - timedelta(days=60)
    chart_start = today_start - timedelta(days=29)

    sales = Order.objects.filter(is_ordered=True).exclude(status__in=SALE_STATUSES_EXCLUDED)

    # ── Revenue KPIs: one query ──────────────────────────────────────────────
    k = sales.aggregate(
        rev_today=Sum('total', filter=Q(date_created__gte=today_start)),
        orders_today=Count('id', filter=Q(date_created__gte=today_start)),
        rev_30=Sum('total', filter=Q(date_created__gte=d30)),
        orders_30=Count('id', filter=Q(date_created__gte=d30)),
        rev_prev=Sum('total', filter=Q(date_created__gte=d60, date_created__lt=d30)),
        orders_prev=Count('id', filter=Q(date_created__gte=d60, date_created__lt=d30)),
    )
    rev_30 = float(k['rev_30'] or 0)
    kpis = {
        'revenue_today': float(k['rev_today'] or 0),
        'orders_today': k['orders_today'],
        'revenue_30': rev_30,
        'orders_30': k['orders_30'],
        'revenue_change': _pct_change(k['rev_30'], k['rev_prev']),
        'orders_change': _pct_change(k['orders_30'], k['orders_prev']),
        'avg_order_30': round(rev_30 / k['orders_30'], 2) if k['orders_30'] else 0,
    }

    # ── Daily sales for the chart (gaps filled with zeros) ───────────────────
    by_day = {
        row['day']: row for row in
        sales.filter(date_created__gte=chart_start)
        .annotate(day=TruncDate('date_created')).values('day')
        .annotate(revenue=Sum('total'), orders=Count('id'))
    }
    chart = {'labels': [], 'revenue': [], 'orders': []}
    for i in range(30):
        day = (chart_start + timedelta(days=i)).date()
        row = by_day.get(day, {})
        chart['labels'].append(day.strftime('%b %d'))
        chart['revenue'].append(float(row.get('revenue') or 0))
        chart['orders'].append(row.get('orders') or 0)

    # ── Orders by status ─────────────────────────────────────────────────────
    counts = dict(Order.objects.filter(is_ordered=True).values_list('status').annotate(c=Count('id')))
    order_status = [
        {'label': label, 'count': counts.get(code, 0), 'badge': STATUS_BADGE.get(code, 'secondary'),
         'url': _changelist('order.Order', is_ordered__exact=1, status__exact=code)}
        for code, label in Order.STATUS_CHOICES
    ]

    # ── Action queue: things a person has to do something about ──────────────
    stale_cutoff = now - timedelta(hours=48)
    stale_processing = (Order.objects.filter(is_ordered=True, status='processing', date_created__lt=stale_cutoff)
                        .annotate(n=Count('shipments')).filter(n=0).count())
    attention = [
        ('vendor.view_vendor', 'Stores awaiting approval', 'fa-store',
         Vendor.objects.filter(status='PENDING').count(), _changelist('vendor.Vendor', status__exact='PENDING')),
        ('product.view_product', 'Products awaiting review', 'fa-box',
         Product.objects.filter(status='in_review').count(), _changelist('product.Product', status__exact='in_review')),
        ('product.view_productreview', 'Reviews awaiting moderation', 'fa-star-half-alt',
         ProductReview.objects.filter(moderation_status=ProductReview.PENDING).count(),
         _changelist('product.ProductReview', moderation_status__exact=ProductReview.PENDING)),
        ('product.view_reviewreport', 'Review reports from sellers', 'fa-flag',
         ReviewReport.objects.filter(status='open').count(), _changelist('product.ReviewReport', status__exact='open')),
        ('order.view_returnrequest', 'Returns received, awaiting refund', 'fa-undo',
         ReturnRequest.objects.filter(status=ReturnRequest.STATUS_RECEIVED).count(),
         _changelist('order.ReturnRequest', status__exact=ReturnRequest.STATUS_RECEIVED)),
        ('order.view_order', 'Processing 48h+ with no shipment', 'fa-shipping-fast',
         stale_processing, _changelist('order.Order', is_ordered__exact=1, status__exact='processing',
                                       date_created__lt=stale_cutoff.strftime('%Y-%m-%d %H:%M:%S'))),
        ('payments.view_payout', 'Failed payouts', 'fa-exclamation-triangle',
         Payout.objects.filter(status='failed').count(), _changelist('payments.Payout', status__exact='failed')),
        ('notification.view_contactinquiry', 'New customer inquiries', 'fa-envelope',
         ContactInquiry.objects.filter(status=ContactInquiry.Status.NEW).count(),
         _changelist('notification.ContactInquiry', status__exact=ContactInquiry.Status.NEW)),
        ('product.view_variants', f'Variants with {LOW_STOCK_THRESHOLD} or fewer in stock', 'fa-cubes',
         Variants.objects.filter(product__status='published', quantity__lte=LOW_STOCK_THRESHOLD).count(),
         _changelist('product.Variants', product__status__exact='published', quantity__lte=LOW_STOCK_THRESHOLD)),
    ]
    attention = [{'perm': p, 'label': l, 'icon': i, 'count': c, 'url': u} for p, l, i, c, u in attention]

    # ── Store snapshot ───────────────────────────────────────────────────────
    users = User.objects.aggregate(total=Count('id'), new_30=Count('id', filter=Q(date_joined__gte=d30)))
    snapshot = {
        'customers': users['total'],
        'new_customers_30': users['new_30'],
        'active_vendors': Vendor.objects.filter(status='VERIFIED', is_suspended=False).count(),
        'published_products': Product.objects.filter(status='published').count(),
    }

    # ── Best sellers, last 30 days ───────────────────────────────────────────
    sold = (OrderProduct.objects
            .filter(order__is_ordered=True, order__date_created__gte=d30)
            .exclude(order__status__in=SALE_STATUSES_EXCLUDED))
    top_products = [
        {'title': r['product__title'] or 'Deleted product', 'revenue': float(r['revenue'] or 0), 'units': r['units'],
         'url': reverse('admin:product_product_change', args=[r['product_id']]) if r['product_id'] else None}
        for r in sold.values('product_id', 'product__title')
        .annotate(revenue=Sum('amount'), units=Sum('quantity')).order_by('-revenue')[:5]
    ]
    top_vendors = [
        {'name': r['product__vendor__name'] or 'Unknown store', 'revenue': float(r['revenue'] or 0), 'orders': r['orders'],
         'url': reverse('admin:vendor_vendor_change', args=[r['product__vendor_id']]) if r['product__vendor_id'] else None}
        for r in sold.values('product__vendor_id', 'product__vendor__name')
        .annotate(revenue=Sum('amount'), orders=Count('order', distinct=True)).order_by('-revenue')[:5]
    ]

    # ── Latest orders ────────────────────────────────────────────────────────
    recent_orders = [
        {'number': o.order_number, 'label': f'#{o.pk}', 'customer': o.user.email if o.user else '—', 'total': float(o.total),
         'badge': STATUS_BADGE.get(o.status, 'secondary'), 'status_label': o.get_status_display(),
         'created': o.date_created,
         'url': reverse('admin:order_order_change', args=[o.pk])}
        for o in Order.objects.filter(is_ordered=True).select_related('user').order_by('-date_created')[:8]
    ]

    return {
        'generated_at': now,
        'kpis': kpis,
        'chart': chart,
        'order_status': order_status,
        'attention': attention,
        'snapshot': snapshot,
        'top_products': top_products,
        'top_vendors': top_vendors,
        'recent_orders': recent_orders,
        'links': {
            'orders': _changelist('order.Order', is_ordered__exact=1),
            'products': _changelist('product.Product'),
            'vendors': _changelist('vendor.Vendor'),
            'customers': _changelist('userauths.User'),
        },
    }


def dashboard_for(user, refresh=False):
    """Cached stats, trimmed to what this user may see."""
    data = None if refresh else cache.get(CACHE_KEY)
    if data is None:
        data = _compute()
        cache.set(CACHE_KEY, data, CACHE_SECONDS)

    can_sales = user.has_perm('order.view_order')
    return {
        **data,
        'can_sales': can_sales,
        'kpis': data['kpis'] if can_sales else None,
        'chart': data['chart'] if can_sales else None,
        'recent_orders': data['recent_orders'] if can_sales else [],
        'top_products': data['top_products'] if can_sales else [],
        'top_vendors': data['top_vendors'] if can_sales else [],
        'attention': [a for a in data['attention'] if user.has_perm(a['perm'])],
        'attention_total': sum(a['count'] for a in data['attention'] if user.has_perm(a['perm'])),
    }

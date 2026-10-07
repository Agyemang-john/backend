"""
vendor/action_center.py
The "what needs my attention" list on the seller home page.

Each check is a small function returning an ActionItem or None. Items are
filtered by the viewer's team capabilities, so staff only see work they can
do, and each check is one or two indexed COUNT queries.
To add a check: write a function, add it to CHECKS with its capability.
"""

from dataclasses import asdict, dataclass
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .access import Capability, capabilities_for_role


@dataclass
class ActionItem:
    key: str
    severity: str      # 'critical' | 'warning' | 'info'
    title: str
    detail: str
    count: int
    link: str


def _plural(n, one, many):
    return one if n == 1 else many


def check_late_orders(vendor):
    from order.fulfilment import late_orders
    n = late_orders(vendor).count()
    if n:
        return ActionItem('late_orders', 'critical', f"{n} {_plural(n, 'order is', 'orders are')} past the ship-by date",
                          "Ship or update them now. Late shipments lower your store rating.", n, '/orders?filter=late')


def check_orders_to_ship(vendor):
    from order.fulfilment import awaiting_dispatch_orders
    n = awaiting_dispatch_orders(vendor).count()
    if n:
        return ActionItem('orders_to_ship', 'warning', f"{n} {_plural(n, 'order', 'orders')} to ship",
                          f"Ship within {vendor.handling_days} {_plural(vendor.handling_days, 'day', 'days')} of the order date.",
                          n, '/orders?filter=to_ship')


def check_returns(vendor):
    from order.models import ReturnRequest
    qs = ReturnRequest.objects.filter(vendor=vendor)
    requested = qs.filter(status=ReturnRequest.STATUS_REQUESTED).count()
    if requested:
        return ActionItem('returns_to_review', 'warning', f"{requested} return {_plural(requested, 'request', 'requests')} to review",
                          "Approve or reject each request. Customers are waiting for a decision.",
                          requested, '/order-returns?status=requested')


def check_stock(vendor):
    from product.models import Product
    threshold = vendor.low_stock_threshold
    live = Product.objects.filter(vendor=vendor, status='published')
    out = live.filter(total_quantity=0).count()
    low = live.filter(total_quantity__gt=0, total_quantity__lte=threshold).count()
    if out:
        return ActionItem('out_of_stock', 'warning', f"{out} {_plural(out, 'product is', 'products are')} out of stock",
                          f"{low} more {_plural(low, 'is', 'are')} running low." if low else "Restock to keep them selling.",
                          out, '/products?stock=out')
    if low:
        return ActionItem('low_stock', 'info', f"{low} {_plural(low, 'product is', 'products are')} running low",
                          f"At or below your threshold of {threshold} units.", low, '/products?stock=low')


def check_rejected_products(vendor):
    from product.models import Product
    n = Product.objects.filter(vendor=vendor, status='rejected').count()
    if n:
        return ActionItem('rejected_products', 'warning', f"{n} {_plural(n, 'product needs', 'products need')} changes",
                          "Read the reviewer's note on each product, fix it and resubmit.", n, '/products?status=rejected')


def check_reviews_to_answer(vendor):
    from product.models import ProductReview
    since = timezone.now() - timedelta(days=30)
    n = ProductReview.objects.filter(vendor=vendor, status=True, rating__lte=3, date__gte=since,
                                     seller_reply='').count()
    if n:
        return ActionItem('reviews_to_answer', 'info', f"{n} critical {_plural(n, 'review', 'reviews')} without a reply",
                          "A short, helpful reply shows other shoppers you care.", n, '/reviews?filter=unanswered')


def check_payout_method(vendor):
    from .models import VendorPaymentMethod
    methods = VendorPaymentMethod.objects.filter(vendor=vendor)
    if not methods.exists():
        return ActionItem('payout_method_missing', 'critical', "Add a payout account",
                          "We can't pay your earnings until you add a Mobile Money or bank account.", 1, '/payment')
    if not methods.filter(status='verified').exists():
        return ActionItem('payout_method_unverified', 'info', "Payout account awaiting verification",
                          "Payouts start once Negromart has verified your account details.", 1, '/payment')


def check_subscription(vendor):
    from payments.models import VendorSubscription
    soon = timezone.now() + timedelta(days=7)
    sub = (VendorSubscription.objects.filter(vendor=vendor, status__in=('active', 'trial'), auto_renew=False,
                                             end_date__lte=soon)
           .order_by('end_date').first())
    if sub:
        return ActionItem('subscription_ending', 'warning', "Your plan ends soon",
                          f"{sub.plan.name} ends on {sub.end_date:%d %b %Y} and won't renew automatically.",
                          1, '/subscribe')


# (check, capability the viewer needs to see it)
CHECKS = [
    (check_late_orders, Capability.MANAGE_ORDERS),
    (check_orders_to_ship, Capability.MANAGE_ORDERS),
    (check_returns, Capability.MANAGE_ORDERS),
    (check_payout_method, Capability.VIEW_FINANCE),
    (check_rejected_products, Capability.MANAGE_CATALOG),
    (check_stock, Capability.MANAGE_CATALOG),
    (check_reviews_to_answer, Capability.MANAGE_CATALOG),
    (check_subscription, Capability.MANAGE_FINANCE),
]

_SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}


def build_action_list(membership):
    """Action items for this team member, most urgent first."""
    allowed = capabilities_for_role(membership.role)
    vendor = membership.vendor
    items = [item for check, cap in CHECKS if cap in allowed and (item := check(vendor))]
    items.sort(key=lambda i: _SEVERITY_ORDER[i.severity])
    return [asdict(i) for i in items]

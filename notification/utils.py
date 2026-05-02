# notification/utils.py
"""
Central helper for creating + broadcasting notifications.

Usage:
    from notification.utils import send_notification

    send_notification(
        recipient=user,
        verb="customer_order_shipped",
        target=order,          # optional
        actor=vendor_user,     # optional
        data={
            "order_number": "INV-ABC123",
            "message": "Your package is on its way!",
            "url": "/dashboard/order-history/42/",
        }
    )
"""

from .models import Notification

# ── Verb metadata ─────────────────────────────────────────────────────────────
# Defines a default title and icon (string key) for each verb.
# The frontend maps the icon key to a real icon component.
VERB_META = {
    # Vendor
    "vendor_new_order":          {"title": "New Order Received",        "icon": "shopping_bag",    "color": "#1565C0"},
    "vendor_order_shipped":      {"title": "Order Shipped",             "icon": "local_shipping",  "color": "#6A1B9A"},
    "vendor_order_cancelled":    {"title": "Order Cancelled",           "icon": "cancel",          "color": "#C62828"},
    "vendor_payout":             {"title": "Payout Processed",          "icon": "payments",        "color": "#2E7D32"},
    "vendor_low_stock":          {"title": "Low Stock Alert",           "icon": "inventory",       "color": "#E65100"},
    "vendor_new_review":         {"title": "New Product Review",        "icon": "star",            "color": "#F9A825"},
    "vendor_product_approved":   {"title": "Product Approved",          "icon": "check_circle",    "color": "#2E7D32"},
    "vendor_product_rejected":   {"title": "Product Rejected",          "icon": "cancel",          "color": "#C62828"},
    "vendor_withdrawal_request": {"title": "Withdrawal Requested",      "icon": "account_balance",  "color": "#1565C0"},
    "vendor_withdrawal_approved":{"title": "Withdrawal Approved",       "icon": "account_balance_wallet", "color": "#2E7D32"},
    # Customer
    "customer_order_placed":     {"title": "Order Placed",              "icon": "receipt_long",    "color": "#1565C0"},
    "customer_order_confirmed":  {"title": "Order Confirmed",           "icon": "check_circle",    "color": "#2E7D32"},
    "customer_order_shipped":    {"title": "Your Order Has Shipped!",   "icon": "local_shipping",  "color": "#6A1B9A"},
    "customer_order_delivered":  {"title": "Order Delivered",           "icon": "celebration",     "color": "#2E7D32"},
    "customer_order_cancelled":  {"title": "Order Cancelled",           "icon": "cancel",          "color": "#C62828"},
    "customer_refund_processed": {"title": "Refund Processed",          "icon": "payments",        "color": "#2E7D32"},
    "customer_tracking_update":  {"title": "Tracking Update",           "icon": "pin_drop",        "color": "#E65100"},
    "customer_price_drop":       {"title": "Price Drop Alert",          "icon": "sell",            "color": "#00695C"},
    "customer_back_in_stock":    {"title": "Back in Stock",             "icon": "inventory_2",     "color": "#1565C0"},
    "customer_wishlist_sale":    {"title": "Wishlist Item on Sale",     "icon": "favorite",        "color": "#AD1457"},
    "customer_review_reminder":  {"title": "Rate Your Purchase",        "icon": "rate_review",     "color": "#F9A825"},
    # Shared
    "message":                   {"title": "New Message",               "icon": "message",         "color": "#1565C0"},
    "announcement":              {"title": "Announcement",              "icon": "campaign",        "color": "#6A1B9A"},
    "support_reply":             {"title": "Support Reply",             "icon": "support_agent",   "color": "#00695C"},
    "verification_update":       {"title": "Verification Update",       "icon": "verified_user",   "color": "#2E7D32"},
    "subscription_reminder":     {"title": "Subscription Expiring",     "icon": "subscriptions",   "color": "#E65100"},
}


def send_notification(recipient, verb, target=None, actor=None, data=None):
    """
    Creates a Notification and broadcasts it via WebSocket.
    Automatically enriches `data` with title/icon/color from VERB_META.
    """
    meta = VERB_META.get(verb, {"title": verb, "icon": "notifications", "color": "#424242"})

    merged_data = {
        "title":  meta["title"],
        "icon":   meta["icon"],
        "color":  meta["color"],
        **(data or {}),
    }

    notification = Notification.objects.create(
        recipient=recipient,
        verb=verb,
        actor=actor,
        target=target,
        data=merged_data,
    )
    # The post_save signal in signals.py handles the WebSocket broadcast.
    return notification

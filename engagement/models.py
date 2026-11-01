from django.conf import settings
from django.db import models


class EmailOptOut(models.Model):
    """
    A shopper said "stop sending me these" for one kind of email. Every
    re-engagement email carries a signed one-click unsubscribe link
    (engagement/emails.py) that writes one of these rows.
    In-app notifications are still created; only email/SMS respect this.
    """
    ALERTS = 'alerts'            # price drops / back in stock
    CART = 'cart'                # abandoned-cart reminders
    CATEGORY_CHOICES = [
        (ALERTS, 'Price and stock alerts'),
        (CART, 'Cart reminders'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_opt_outs')
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'category'], name='uniq_email_opt_out'),
        ]

    def __str__(self):
        return f"{self.user_id} opted out of {self.category}"


class ProductAlert(models.Model):
    """
    A shopper watching one product (or one size/colour of it).

    PRICE alerts stay on: each time the price reaches a new low at least
    MIN_DROP_PERCENT below `reference_price` (or reaches `target_price`), the
    shopper hears about it once and the reference moves down to that price.
    Wishlisting a product creates one automatically (source=WISHLIST) and
    removing it from the wishlist removes it.

    STOCK alerts fire once, when an out-of-stock item comes back, and then
    switch off.

    The sweep (engagement/alerts.py) compares live price/stock with
    `last_seen_*`, so it catches every way prices and stock change: seller
    edits, spreadsheet updates, returns put back on the shelf, flash sales.
    """
    PRICE = 'price'
    STOCK = 'stock'
    KIND_CHOICES = [(PRICE, 'Price drop'), (STOCK, 'Back in stock')]

    MANUAL = 'manual'
    WISHLIST = 'wishlist'
    SOURCE_CHOICES = [(MANUAL, 'Asked for it'), (WISHLIST, 'From wishlist')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='product_alerts')
    product = models.ForeignKey('product.Product', on_delete=models.CASCADE, related_name='alerts')
    variant = models.ForeignKey('product.Variants', on_delete=models.CASCADE, null=True, blank=True, related_name='alerts')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=MANUAL)

    # Price alerts: notify at or below target_price, or on a new low below reference_price.
    target_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    reference_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # What the sweep saw last time, so a change is noticed once.
    last_seen_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    last_seen_in_stock = models.BooleanField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    sent_count = models.PositiveIntegerField(default=0)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # nulls_distinct=False: one product-wide alert per kind too (variant NULL).
            models.UniqueConstraint(
                fields=['user', 'product', 'variant', 'kind'],
                name='uniq_product_alert', nulls_distinct=False,
            ),
        ]
        indexes = [
            # The sweep walks active alerts grouped by item.
            models.Index(fields=['is_active', 'product', 'variant'], name='alert_sweep_idx'),
            models.Index(fields=['user', 'is_active'], name='alert_user_idx'),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} alert: user {self.user_id} on product {self.product_id}"


class CartReminder(models.Model):
    """
    One "you left something in your cart" message. `fingerprint` identifies
    the cart contents, so each version of a cart gets at most one message per
    stage: change the cart and the cycle starts again. `order` is filled in
    when the shopper orders within ATTRIBUTION_DAYS (engagement/cart_reminders.py).
    """
    FIRST = 1      # ~1 hour idle
    SECOND = 2     # ~1 day idle
    STAGE_CHOICES = [(FIRST, 'First reminder'), (SECOND, 'Second reminder')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='cart_reminders')
    stage = models.PositiveSmallIntegerField(choices=STAGE_CHOICES)
    fingerprint = models.CharField(max_length=40)
    item_count = models.PositiveIntegerField(default=0)
    cart_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    emailed = models.BooleanField(default=False)
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    order = models.ForeignKey('order.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    converted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'fingerprint', 'stage'], name='uniq_cart_reminder_stage'),
        ]
        indexes = [models.Index(fields=['user', '-sent_at'], name='cart_reminder_user_idx')]

    def __str__(self):
        return f"Cart reminder {self.stage} to user {self.user_id}"

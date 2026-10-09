"""
Order models for the e-commerce platform.

Defines the core order-related data structures including Cart, CartItem, Order,
OrderProduct, DeliveryRate, Shipment, TrackingEvent, and CampusZone. Handles
cart management, order lifecycle, delivery fee calculation, shipment tracking,
and campus-zone-based delivery logic.
"""

import logging
import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import mark_safe
from django.contrib.auth import get_user_model
from decimal import Decimal

from product.models import *
from product.utils import *
from address.models import *
from vendor.models import *
from .service import FeeCalculator, FeeResult
from userauths.models import Profile

logger = logging.getLogger(__name__)

PAYMENT_STATUS = (
    ('received', 'Received'),
    ('approved', 'Approved'),
    ('success', 'Success'),
    ('accepted', 'Accepted'),
    ('canceled', 'Canceled'),
)

User = get_user_model()

class CartManager(models.Manager):
    def get_for_request(self, request):
        """Get existing cart for the request (user or session) without creating a new one."""
        if request.user.is_authenticated:
            try:
                return self.get(user=request.user)
            except Cart.DoesNotExist:
                return None
        
        return None

    def create_for_request(self, request):
        """Create a new cart for the request (user or session)."""
        cart, created = self.get_or_create(user=request.user)
        return cart
    
    def get_or_create_for_request(self, request):
        if request.user.is_authenticated:
            cart_qs = self.prefetch_related('cart_items__product', 'cart_items__variant')
            cart, created = cart_qs.get_or_create(user=request.user)
            return cart
        return None


class Cart(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CartManager()

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        if self.user and self.user.email:
            return f"Cart (User: {self.user.email})"
        # Must always return a str: None here crashed every admin page that
        # shows a guest cart ("__str__ returned non-string").
        return f"Guest cart #{self.pk}"

    @property
    def is_guest_cart(self):
        return self.user is None 
    
    @property
    def total_quantity(self):
        """
        Calculate the total quantity of all items in the cart.
        """
        return sum(item.quantity for item in self.cart_items.all()) if hasattr(self, 'cart_items') else 0

    @property
    def total_price(self):
        return sum(item.amount for item in self.cart_items.all())

    @property
    def total_items(self):
        return self.cart_items.count()
    
    def calculate_total_delivery_fee(self):
        address = Address.objects.filter(user=self.user, status=True).first()
        if not address or address.latitude is None or address.longitude is None:
            logger.warning(f"No valid default address for user {self.user.email if self.user else 'anonymous'}. Falling back to zero delivery fee.")
            return Decimal(0)
        
        # Get buyer country: Address > Profile > 'GH'
        user_profile = Profile.objects.filter(user=self.user).first()
        buyer_country = address.country if address and address.country else \
                        user_profile.country if user_profile and user_profile.country else 'GH'
        
        fee_result = FeeCalculator.calculate_total_delivery_fee(self.cart_items.all(), address, buyer_country_code=buyer_country)
        return fee_result.total

    def calculate_grand_total(self):
        return Decimal(self.total_price) + self.calculate_total_delivery_fee()
    
    def calculate_packaging_fees(self):
        """Calculate total packaging fees."""
        return Decimal(sum(item.packaging_fee() for item in self.cart_items.all()))
    
    def check_address_region(self, user_profile):
        """
        Check if the user's address region is in the available regions for each product in the cart.
        If not, raise a validation error or remove the product from the cart.
        """
        user_region = user_profile.contry

        # Go through each cart item and check the product's available regions
        for item in self.cart_items.all():
            product = item.product

            # Check if the product has available regions
            if product.available_in_regions.exists():
                # Check if the user's region is in the available regions for the product
                if not product.available_in_regions.filter(name=user_region).exists():
                    # You can either remove the item from the cart or raise an error
                    self.cart_items.filter(id=item.id).delete()  # Option 1: Remove the item from the cart
                    # raise ValidationError(f"The product '{product.title}' is not available in your region: {user_region}")  # Option 2: Raise error
    
    def prevent_checkout_unavailable_products(self, user_profile):
        """
        Deletes cart items if the user's address region is not in the available regions for any product.
        Returns a list of deleted items for frontend notification.
        """
        user_region = user_profile.country
        deleted_items = []

        # Go through each cart item and check the product's available regions
        for item in self.cart_items.all():
            product = item.product

            # Check if the product has available regions
            if product.available_in_regions.exists():
                # If the user's region is not in the product's available regions, mark for deletion
                if not product.available_in_regions.filter(Q(name__iexact=user_region) | Q(name__icontains=user_region)).exists():
                    deleted_items.append({
                        'product_title': product.title,
                        'region': user_region
                    })
                    item.delete() 

        return deleted_items
                
# CartItem model
class CartItem(models.Model):
    cart = models.ForeignKey(Cart, related_name='cart_items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    variant = models.ForeignKey(Variants, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.IntegerField(default=1)
    url = models.CharField(max_length=200, null=True, blank=True)
    added = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivery_option = models.ForeignKey(
        DeliveryOption, on_delete=models.SET_NULL, null=True, blank=True
    )
    # Legacy: the flash price used to be locked in here on first add, which kept
    # it after the sale ended. Pricing now comes from the live sale
    # (order/pricing.py); this column is no longer read or written.
    flash_sale_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    def __str__(self):
        product = self.product.title if self.product else "Deleted product"
        owner = self.cart.user.email if self.cart.user else f"guest cart #{self.cart_id}"
        return f"CartItem for {owner} - Product: {product}"

    class Meta:
        ordering = ('-created_at',)

    @cached_property
    def pricing(self):
        """Live flash-sale aware pricing for this line (see order/pricing.py)."""
        from order.pricing import line_pricing
        return line_pricing(self.product, self.variant, self.quantity)

    @property
    def flash_sale(self):
        return self.pricing['flash_sale']

    @property
    def price(self):
        return self.pricing['unit_price']

    @property
    def amount(self):
        return self.pricing['amount']

    def packaging_fee(self):
        return calculate_packaging_fee(self.product.weight, self.product.volume) * self.quantity
    
    @property
    def selected_delivery_option(self):
        """
        Get the selected delivery option. If not set, fallback to the default option for the product.
        """
        if self.delivery_option:
            return self.delivery_option
        # Fallback to the default option for the product
        product_delivery_option = ProductDeliveryOption.objects.filter(
            product=self.product, variant=self.variant, default=True
        ).first()
        return product_delivery_option.delivery_option if product_delivery_option else None

    def item_image(self):
        return mark_safe('<img src="%s" width="50" height="50" />' % (self.product.image.url))

    
class DeliveryRate(models.Model):
    rate_per_km = models.DecimalField(max_digits=5, decimal_places=2, default=2.00)
    base_price = models.DecimalField(max_digits=5, decimal_places=2, default=13.00)

    def __str__(self):
        return f"{self.rate_per_km} GHS per km"


class Order(models.Model):
    PAYMENT_METHOD = (
        ('cash_on_delivery', 'Cash on Delivery'),
        ('paypal', 'PayPal'),
        ('paystack', 'Paystack'),
        ('bank_transfer', 'Bank Transfer'),
    )
    
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('partially_delivered', 'Partially Delivered'),
        ('delivered', 'Delivered'),
        ('canceled', 'Canceled'),
    )

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    vendors = models.ManyToManyField(Vendor, blank=True)
    order_number = models.CharField(max_length=390, editable=False)
    payment_id = models.CharField(max_length=200, null=True, blank=True, editable=False)
    address = models.ForeignKey(Address, on_delete=models.SET_NULL, null=True, blank=True)
    payment_method = models.CharField(max_length=30, choices=PAYMENT_METHOD, default='paystack')
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    ip = models.CharField(blank=True, max_length=20)
    adminnote = models.CharField(blank=True, max_length=100)
    is_ordered = models.BooleanField(default=False)
    response_date = models.DateTimeField(null=True, blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-date_created',)

    def order_placed_to(self):
        return ", ".join([str(vendor) for vendor in self.vendors.all()])

    def __str__(self):
        user_email = self.user.email if self.user else "Deleted User"
        return f"Order {self.order_number} by {user_email}"
    
    @property
    def total_price(self):
        return sum(item.amount for item in self.order_products.all())
    
    def calculate_total_delivery_fee(self):
        if not hasattr(self.address, 'latitude') or not hasattr(self.address, 'longitude') or self.address.latitude is None or self.address.longitude is None:
            logger.warning(f"Order {self.order_number} has no valid address coordinates. Falling back to zero delivery fee.")
            return Decimal(0)
        return FeeCalculator.calculate_total_delivery_fee(self.order_products.all(), self.address, item_type='order')

    def calculate_grand_total(self):
        return Decimal(self.total_price) + self.calculate_total_delivery_fee().total
    
    def calculate_packaging_fees(self):
        """Calculate total packaging fees."""
        return sum(item.packaging_fee() for item in self.order_products.all())
    
    def get_overall_delivery_range(self):
        """
        Calculate the overall delivery range for the order based on OrderProducts.
        """
        order_products = self.order_products.all()
        if not order_products.exists():
            logger.warning(f"No order products found for order {self.order_number}")
            return None

        min_date = None
        max_date = None

        for product in order_products:
            delivery_range = product.get_delivery_range()
            if not delivery_range or "Overdue" in delivery_range:
                continue  # Skip invalid or overdue ranges

            # Parse the delivery range string to extract dates
            if delivery_range == "Today":
                delivery_date = timezone.now().created_at()
                min_date = min_date or delivery_date
                max_date = max_date or delivery_date
                min_date = min(min_date, delivery_date)
                max_date = max(max_date, delivery_date)
            elif delivery_range.startswith("Overdue"):
                continue  # Skip overdue deliveries for overall range
            else:
                try:
                    # Handle single date or range (e.g., "Sep 25, 2025" or "Sep 25, 2025 to Sep 27, 2025")
                    parts = delivery_range.split(" to ")
                    from_date = parts[0]
                    to_date = parts[-1]
                    from_date = timezone.datetime.strptime(from_date, "%b %d, %Y").date() if from_date != "Today" else timezone.now().date()
                    to_date = timezone.datetime.strptime(to_date, "%b %d, %Y").date() if to_date != "Today" else timezone.now().date()
                    min_date = min_date or from_date
                    max_date = max_date or to_date
                    min_date = min(min_date, from_date)
                    max_date = max(max_date, to_date)
                except ValueError as e:
                    logger.error(f"Error parsing delivery range for order {self.order_number}: {delivery_range}, {str(e)}")
                    continue

        if not min_date or not max_date:
            return None

        today = timezone.now().date()
        if max_date < today:
            return f"Overdue (expected by {max_date.strftime('%b %d, %Y')})"

        from_date = "Today" if min_date == today else min_date.strftime("%b %d, %Y")
        to_date = "Today" if max_date == today else max_date.strftime("%b %d, %Y")
        return f"{from_date}" if from_date == to_date else f"{from_date} to {to_date}"

    def get_vendor_delivery_date_range(self, vendor):
        """
        Calculate the delivery date range for a specific vendor in the order.
        """
        order_products = self.order_products.filter(product__vendor=vendor)
        if not order_products.exists():
            logger.warning(f"No order products found for vendor {vendor} in order {self.order_number}")
            return None

        min_date = None
        max_date = None

        for order_product in order_products:
            delivery_range = order_product.get_delivery_range()
            if not delivery_range or "Overdue" in delivery_range:
                continue  # Skip invalid or overdue ranges

            if delivery_range == "Today":
                delivery_date = timezone.now().date()
                min_date = min_date or delivery_date
                max_date = max_date or delivery_date
                min_date = min(min_date, delivery_date)
                max_date = max(max_date, delivery_date)
            else:
                try:
                    parts = delivery_range.split(" to ")
                    from_date = parts[0]
                    to_date = parts[-1]
                    from_date = timezone.datetime.strptime(from_date, "%b %d, %Y").date() if from_date != "Today" else timezone.now().date()
                    to_date = timezone.datetime.strptime(to_date, "%b %d, %Y").date() if to_date != "Today" else timezone.now().date()
                    min_date = min_date or from_date
                    max_date = max_date or to_date
                    min_date = min(min_date, from_date)
                    max_date = max(max_date, to_date)
                except ValueError as e:
                    logger.error(f"Error parsing delivery range for vendor {vendor} in order {self.order_number}: {delivery_range}, {str(e)}")
                    continue

        if not min_date or not max_date:
            return "Delivery date unavailable"

        today = timezone.now().date()
        if max_date < today:
            return f"Overdue (expected by {max_date.strftime('%b %d, %Y')})"

        from_date = "Today" if min_date == today else min_date.strftime("%b %d, %Y")
        to_date = "Today" if max_date == today else max_date.strftime("%b %d, %Y")
        return f"{from_date}" if from_date == to_date else f"{from_date} to {to_date}"

    def get_vendor_total(self, vendor):
        """Calculate the total amount for a specific vendor in this order."""
        order_products = self.order_products.filter(product__vendor=vendor)
        return sum(op.amount for op in order_products)

    def get_vendor_delivery_cost(self, vendor):
        """Calculate the total delivery cost for a specific vendor in this order."""
        order_products = self.order_products.filter(product__vendor=vendor)
        return sum(op.selected_delivery_option.cost for op in order_products if op.selected_delivery_option)
    
    def calculate_vendor_delivery_fee(self, vendor):
        if not hasattr(self.address, 'latitude') or not hasattr(self.address, 'longitude') or self.address.latitude is None or self.address.longitude is None:
            logger.warning(f"Order {self.order_number} has no valid address coordinates for vendor {vendor}. Falling back to zero delivery fee.")
            return FeeResult(total=Decimal(0), dynamic_quotes={}, invalid_items=[])

        items = self.order_products.filter(product__vendor=vendor)
        if not items.exists():
            return FeeResult(total=Decimal(0), dynamic_quotes={}, invalid_items=[])

        return FeeCalculator.calculate_total_delivery_fee(items, self.address, item_type='order')

    def calculate_vendor_grand_total(self, vendor):
        vendor_total = self.get_vendor_total(vendor)
        vendor_delivery_fee = self.calculate_vendor_delivery_fee(vendor)
        return vendor_total + vendor_delivery_fee.total

    # def get_shipments(self):
    #     return self.shipments.all().prefetch_related('tracking_events', 'items__product')

    # def get_overall_status(self):
    #     shipments = self.shipments.all()
    #     if not shipments:
    #         return "Pending"
    #     if all(s.status == 'delivered' for s in shipments):
    #         return "Delivered"
    #     if any(s.status == 'delivered' for s in shipments):
    #         return "Partially Delivered"
    #     if any(s.status in ['in_transit', 'out_for_delivery'] for s in shipments):
    #         return "Shipped"
    #     return "Processing"

    # def get_tracking_summary(self):
    #     return [
    #         {
    #             'shipment_id': s.shipment_id,
    #             'vendor': s.vendor.name,
    #             'carrier': s.carrier,
    #             'tracking_number': s.tracking_number,
    #             'tracking_url': s.tracking_url,
    #             'status': s.get_status_display(),
    #             'estimated_delivery': s.estimated_delivery_date,
    #             'progress': s.progress_percentage,
    #             'latest_event': s.latest_event.description if s.latest_event else None,
    #             'items': [op.product.title for op in s.items.all()]
    #         }
    #         for s in self.shipments.all()
    #     ]

class OrderProduct(models.Model):
    order = models.ForeignKey(Order, related_name='order_products', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    variant = models.ForeignKey(Variants, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    tracking_number = models.CharField(max_length=100, null=True, blank=True, editable=False)
    status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('canceled', 'Canceled'),
    ], default="pending")

    selected_delivery_option = models.ForeignKey(
        DeliveryOption,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_products",
    )
    shipped_date = models.DateTimeField(null=True, blank=True)
    delivered_date = models.DateTimeField(null=True, blank=True)
    refund_reason = models.CharField(max_length=200, null=True, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-date_created',)

    def packaging_fee(self):
        if not self.product:
            return Decimal(0)
        return calculate_packaging_fee(self.product.weight, self.product.volume) * self.quantity

    def save(self, *args, **kwargs):
        self.amount = Decimal(self.quantity) * self.price
        super().save(*args, **kwargs)

    def get_delivery_range(self):
        if not self.selected_delivery_option:
            if not self.product:
                return None
            product_delivery_option = ProductDeliveryOption.objects.filter(
                product=self.product, variant=self.variant, default=True
            ).first()
            if product_delivery_option and product_delivery_option.delivery_option:
                return product_delivery_option.get_delivery_date_range(self.date_created)
            logger.warning(f"No delivery option for OrderProduct (product: {self.product.title})")
            return None
        return self.selected_delivery_option.get_delivery_date_range(self.date_created)

    def get_delivery_status(self):
        if not self.selected_delivery_option:
            if not self.product:
                return "Product no longer available"
            product_delivery_option = ProductDeliveryOption.objects.filter(
                product=self.product, variant=self.variant, default=True
            ).first()
            if product_delivery_option and product_delivery_option.delivery_option:
                return product_delivery_option.delivery_option.get_delivery_status(self.date_created)
            return "Delivery option unavailable"
        return self.selected_delivery_option.get_delivery_status(self.date_created)

    def __str__(self):
        product_name = self.product.title if self.product else "Deleted Product"
        return f"{product_name} (Order {self.order.order_number})"
    

class Refund(models.Model):
    order_product = models.ForeignKey(OrderProduct, on_delete=models.CASCADE)
    amount = models.FloatField()
    reason = models.TextField()
    date = models.DateTimeField(auto_now_add=True)

from django.utils import timezone
import uuid

class Shipment(models.Model):
    """
    One shipment = All items from one vendor going to one address
    This is what gets a tracking number and carrier
    """
    shipment_id = models.CharField(max_length=50, unique=True, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='shipments')
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Which OrderProducts are in this shipment
    items = models.ManyToManyField('OrderProduct', related_name='shipments')

    # Who moves the parcel. `provider` is a key in settings.DELIVERY_PROVIDERS
    # (order/delivery/), so adding an external courier later is a new provider
    # class plus a settings entry, not a schema change. Today Negromart
    # delivers everything itself ('platform').
    FULFILLED_BY_CHOICES = [
        ('platform', 'Negromart delivery'),
        ('seller', 'Seller delivers'),
        ('carrier', 'External courier'),
    ]
    provider = models.CharField(max_length=40, default='platform', db_index=True)
    fulfilled_by = models.CharField(max_length=20, choices=FULFILLED_BY_CHOICES, default='platform')
    external_reference = models.CharField(max_length=120, blank=True, default='',
                                          help_text="The courier's own id for this shipment.")
    label_url = models.URLField(blank=True, default='')
    provider_data = models.JSONField(default=dict, blank=True,
                                     help_text="Raw provider response, kept for support/debugging.")
    # Set once the seller's earnings for this shipment are in the ledger
    # (payments/ledger.py). Makes posting idempotent and easy to sweep.
    ledger_posted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Carrier & Tracking
    carrier = models.CharField(max_length=100, blank=True)  # e.g., "DHL", "FedEx", "Aramex"
    carrier_code = models.CharField(max_length=20, blank=True)  # for API: "dhl", "fedex"
    tracking_number = models.CharField(max_length=100, blank=True, null=True)
    tracking_url = models.URLField(blank=True, null=True)

    # Status (mirrors carrier status when synced)
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('label_created', 'Label Created'),
        ('in_transit', 'In Transit'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('failed', 'Delivery Failed'),
        ('canceled', 'Canceled'),
        ('returned', 'Returned'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Dates
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    estimated_delivery_date = models.DateField(null=True, blank=True)

    # International?
    is_international = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('order', 'vendor')]
        indexes = [
            # The ledger sweep looks for "delivered but not yet posted".
            models.Index(fields=['status', 'ledger_posted_at'], name='shipment_ledger_sweep_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self.shipment_id:
            self.shipment_id = f"SH-{uuid.uuid4().hex[:10].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        vendor_name = self.vendor.name if self.vendor else "Deleted Vendor"
        return f"{self.shipment_id} - {vendor_name} - {self.tracking_number or 'No tracking'}"

    @property
    def latest_event(self):
        return self.tracking_events.order_by('-event_date').first()

    @property
    def progress_percentage(self):
        # Simple progress estimation
        # .all() + sort in Python so a prefetch_related('tracking_events') is used
        # (the admin lists many shipments; ordering in SQL re-queried per row).
        events = sorted(self.tracking_events.all(), key=lambda e: e.event_date)
        if not events:
            return 0
        # Very rough: delivered = 100%, in transit = 60%, etc.
        latest = events[-1]
        mapping = {
            'delivered': 100,
            'out_for_delivery': 90,
            'in_transit': 60,
            'label_created': 30,
            'pending': 10,
        }
        return mapping.get(latest.status, 20)


class TrackingEvent(models.Model):
    """
    Real-time events from carrier (via webhook or polling)
    Like Amazon's timeline
    """
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name='tracking_events')
    
    STATUS_CHOICES = [
        ('info', 'Info Received'),
        ('in_transit', 'In Transit'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('exception', 'Delivery Exception'),
        ('failed_attempt', 'Failed Delivery Attempt'),
        ('returned_to_sender', 'Returned to Sender'),
    ]
    
    status = models.CharField(max_length=30, choices=STATUS_CHOICES)
    description = models.CharField(max_length=500)
    location = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    
    event_date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-event_date']

    def __str__(self):
        return f"{self.get_status_display()} - {self.event_date.strftime('%b %d, %Y %H:%M')}"


# class CityDeliveryRate(models.Model):
#     from_city = models.CharField(max_length=100)
#     to_city   = models.CharField(max_length=100)
#     flat_fee  = models.DecimalField(max_digits=10, decimal_places=2)

#     class Meta:
#         unique_together = ('from_city', 'to_city')


class CampusZone(models.Model):
    name         = models.CharField(max_length=100)       # e.g. "KNUST"
    center_lat   = models.FloatField()
    center_lon   = models.FloatField()
    radius_km    = models.FloatField(default=2.0)         # campus boundary radius
    flat_fee     = models.DecimalField(max_digits=8, decimal_places=2, default=5.00)
    free_delivery_threshold = models.DecimalField(        # free delivery if order total >= this
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    def __str__(self):
        return self.name


# ─────────────────────────────────────────────────────────────────────────────
# Returns
# ─────────────────────────────────────────────────────────────────────────────
# One request per order line. The customer opens it, the seller approves or
# rejects it, the item comes back (by Negromart delivery today; a provider
# later), the seller confirms receipt, and Negromart issues the refund, which
# also debits the seller's ledger. Rules live in order/returns.py.

class ReturnRequest(models.Model):
    # Reasons where the seller is at fault are always returnable within the
    # platform's minimum window, even if the product's own return period is 0.
    REASON_CHOICES = [
        ('damaged', 'Arrived damaged'),
        ('defective', 'Does not work / defective'),
        ('wrong_item', 'Wrong item sent'),
        ('not_as_described', 'Not as described'),
        ('missing_parts', 'Missing parts or accessories'),
        ('changed_mind', 'No longer needed'),
        ('other', 'Other'),
    ]
    SELLER_FAULT_REASONS = frozenset({'damaged', 'defective', 'wrong_item', 'not_as_described', 'missing_parts'})

    STATUS_REQUESTED = 'requested'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_RECEIVED = 'received'
    STATUS_REFUNDED = 'refunded'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_REQUESTED, 'Requested'),
        (STATUS_APPROVED, 'Approved, awaiting return'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_RECEIVED, 'Item received'),
        (STATUS_REFUNDED, 'Refunded'),
        (STATUS_CANCELLED, 'Cancelled by customer'),
    ]
    OPEN_STATUSES = (STATUS_REQUESTED, STATUS_APPROVED, STATUS_RECEIVED)

    reference = models.CharField(max_length=20, unique=True, editable=False)
    order_product = models.ForeignKey(OrderProduct, on_delete=models.PROTECT, related_name='return_requests')
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='return_requests')
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, related_name='return_requests')
    customer = models.ForeignKey(get_user_model(), on_delete=models.SET_NULL, null=True, related_name='return_requests')

    reason = models.CharField(max_length=30, choices=REASON_CHOICES)
    details = models.TextField(max_length=2000, blank=True, default='')
    quantity = models.PositiveIntegerField(default=1)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REQUESTED, db_index=True)
    seller_note = models.TextField(max_length=1000, blank=True, default='')
    # Pickup of the returned item, when a delivery provider handles it.
    return_shipment = models.ForeignKey(Shipment, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(get_user_model(), on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    received_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['vendor', 'status']),
            models.Index(fields=['customer', '-created_at']),
        ]
        constraints = [
            # At most one active return per order line.
            models.UniqueConstraint(
                fields=['order_product'],
                condition=Q(status__in=('requested', 'approved', 'received')),
                name='uniq_open_return_per_order_line',
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"RT-{uuid.uuid4().hex[:10].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} ({self.get_status_display()})"

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

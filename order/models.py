from django.db import models
from django.forms import ModelForm
from product.models import *
from django.utils.html import mark_safe
from address.models import *
from vendor.models import *
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
import uuid
from product.utils import *
from .service import FeeCalculator, FeeResult


# Create your models here.

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
        """Get or create a cart for the request."""
        if request.user.is_authenticated:
            cart, created = self.get_or_create(user=request.user)
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
        Prevent checkout if the user's address region is not in the available regions for any product.
        Raises a ValidationError if any product is unavailable in the user's region.
        """
        user_region = user_profile.country

        # Go through each cart item and check the product's available regions
        for item in self.cart_items.all():
            product = item.product

            # Check if the product has available regions
            if product.available_in_regions.exists():
                # If the user's region is not in the product's available regions, raise an error
                if not product.available_in_regions.filter(name=user_region).exists():
                    raise ValidationError(f"The product '{product.title}' is not available in your region: {user_region}")
                
# CartItem model
class CartItem(models.Model):
    cart = models.ForeignKey(Cart, related_name='cart_items', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    variant = models.ForeignKey(Variants, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.IntegerField(default=1)
    url = models.CharField(max_length=200, null=True, blank=True)
    added = models.BooleanField(default=True)
    date = models.DateTimeField(auto_now=True)
    delivery_option = models.ForeignKey(
        DeliveryOption, on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        if self.cart.user:
            return f"CartItem for {self.cart.user.email} - Product: {self.product.title}"

    @property
    def price(self):
        return self.variant.price if self.variant else self.product.price
    
    @property
    def amount(self):
        return Decimal(self.quantity) * self.price

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
        ('delivered', 'Delivered'),
        ('canceled', 'Canceled'),
    )

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    vendors = models.ManyToManyField(Vendor, blank=True)
    order_number = models.CharField(max_length=390, editable=False)
    payment_id = models.CharField(max_length=200, null=True, blank=True, editable=False)
    address = models.ForeignKey(Address, on_delete=models.CASCADE)
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
        return f"Order {self.order_number} by {self.user.email}"
    
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
                delivery_date = timezone.now().date()
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

class OrderProduct(models.Model):
    order = models.ForeignKey(Order, related_name='order_products', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    variant = models.ForeignKey(Variants, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
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
    tracking_number = models.CharField(max_length=100, null=True, blank=True)
    refund_reason = models.CharField(max_length=200, null=True, blank=True)

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-date_created',)

    def packaging_fee(self):
        return calculate_packaging_fee(self.product.weight, self.product.volume) * self.quantity

    def save(self, *args, **kwargs):
        self.amount = Decimal(self.quantity) * self.price  # Calculate amount
        super().save(*args, **kwargs)
    
    def get_delivery_range(self):
        """
        Get the delivery date range for this OrderProduct, using date_created as reference.
        """
        if not self.selected_delivery_option:
            # Fallback to default delivery option
            product_delivery_option = ProductDeliveryOption.objects.filter(
                product=self.product, variant=self.variant, default=True
            ).first()
            if product_delivery_option and product_delivery_option.delivery_option:
                return product_delivery_option.get_delivery_date_range(self.date_created)
            logger.warning(f"No delivery option for OrderProduct (product: {self.product.title})")
            return None
        return self.selected_delivery_option.get_delivery_date_range(self.date_created)

    def get_delivery_status(self):
        """
        Get the delivery status for this OrderProduct.
        """
        if not self.selected_delivery_option:
            # Fallback to default delivery option
            product_delivery_option = ProductDeliveryOption.objects.filter(
                product=self.product, variant=self.variant, default=True
            ).first()
            if product_delivery_option and product_delivery_option.delivery_option:
                return product_delivery_option.delivery_option.get_delivery_status(self.date_created)
            return "Delivery option unavailable"
        return self.selected_delivery_option.get_delivery_status(self.date_created)
    

    def __str__(self):
        return f"{self.product.title} (Order {self.order.order_number})"
    

class Refund(models.Model):
    order_product = models.ForeignKey(OrderProduct, on_delete=models.CASCADE)
    amount = models.FloatField()
    reason = models.TextField()
    date = models.DateTimeField(auto_now_add=True)

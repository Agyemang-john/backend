from django.db import models
from django.forms import ModelForm
from product.models import *
from django.utils.html import mark_safe
from address.models import *
from vendor.models import *
from decimal import Decimal
from .service import calculate_delivery_fee
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
import uuid
from product.utils import *


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
    
    
    def calculate_packaging_fees(self):
        """Calculate total packaging fees."""
        return sum(item.packaging_fee() for item in self.cart_items.all())
    
    def calculate_total_delivery_fee(self, user_profile):
        """
        Calculate the total delivery fee based on vendors and their respective locations.
        Each vendor is processed once.
        """
        processed_vendors = set()
        total_delivery_fee = Decimal(0)
        vendor_delivery_fees = {}

        packaging_fees = Decimal(self.calculate_packaging_fees())

        for item in self.cart_items.all():

            product_delivery_option = ProductDeliveryOption.objects.filter(
                product=item.product, default=True
            ).first()

            # Fallback if no default option is defined
            if not product_delivery_option:
                product_delivery_option = ProductDeliveryOption.objects.filter(product=item.product).first()


            product = item.product
            vendor = product.vendor
            # delivery_option_cost = item.delivery_option.cost if item.delivery_option else product_delivery_option.delivery_option.cost
            if item.delivery_option:
                delivery_option_cost = item.delivery_option.cost
            elif product_delivery_option and product_delivery_option.delivery_option:
                delivery_option_cost = product_delivery_option.delivery_option.cost
            else:
                raise ValidationError(f"No delivery option available for product: {item.product.title}")



            # Retrieve delivery options for the product that are set to default


            if vendor not in processed_vendors:
                # Calculate the delivery fee based on the vendor's location
                delivery_fee = calculate_delivery_fee(
                    vendor.about.latitude, vendor.about.longitude,
                    user_profile.latitude, user_profile.longitude,
                    delivery_option_cost  # Use the first product's delivery option cost for calculation
                )
                total_delivery_fee += Decimal(delivery_fee)
                vendor_delivery_fees[vendor.id] = Decimal(delivery_fee)
                processed_vendors.add(vendor)
            else:
                # If the vendor has already been processed, add only the delivery option cost
                total_delivery_fee += Decimal(delivery_option_cost)

        return total_delivery_fee + packaging_fees
    
    
    def calculate_grand_total(self, user_profile):
        """
        Calculate the grand total amount for the cart.
        """
        total_amount = Decimal(self.total_price)  # Convert total_price to Decimal
        # packaging_fees = Decimal(self.calculate_packaging_fees())  # Convert packaging fees to Decimal
        delivery_fees = Decimal(self.calculate_total_delivery_fee(user_profile))  # Convert delivery fees to Decimal

        grand_total = total_amount + delivery_fees
        return grand_total
    
    def check_address_region(self, user_profile):
        """
        Check if the user's address region is in the available regions for each product in the cart.
        If not, raise a validation error or remove the product from the cart.
        """
        user_region = user_profile.region

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
        user_region = user_profile.region

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
        return self.quantity * self.price

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
    total = models.FloatField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    ip = models.CharField(blank=True, max_length=20)
    adminnote = models.CharField(blank=True, max_length=100)
    is_ordered = models.BooleanField(default=False)
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
    
    def calculate_packaging_fees(self):
        """Calculate total packaging fees."""
        return sum(item.packaging_fee() for item in self.order_products.all())
    
    def calculate_total_delivery_fee(self, user_profile):
        """
        Calculate the total delivery fee based on vendors and their respective locations.
        Each vendor is processed once.
        """
        processed_vendors = set()
        total_delivery_fee = Decimal(0)
        vendor_delivery_fees = {}

        packaging_fees = Decimal(self.calculate_packaging_fees())

        for item in self.order_products.all():

            product_delivery_option = ProductDeliveryOption.objects.filter(
                product=item.product, default=True
            ).first()

            product = item.product
            vendor = product.vendor
            delivery_option_cost = item.selected_delivery_option.cost if item.selected_delivery_option else product_delivery_option.delivery_option.cost

            # Retrieve delivery options for the product that are set to default


            if vendor not in processed_vendors:
                # Calculate the delivery fee based on the vendor's location
                delivery_fee = calculate_delivery_fee(
                    vendor.about.latitude, vendor.about.longitude,
                    user_profile.latitude, user_profile.longitude,
                    delivery_option_cost  # Use the first product's delivery option cost for calculation
                )
                total_delivery_fee += Decimal(delivery_fee)
                vendor_delivery_fees[vendor.id] = Decimal(delivery_fee)
                processed_vendors.add(vendor)
            else:
                # If the vendor has already been processed, add only the delivery option cost
                total_delivery_fee += Decimal(delivery_option_cost)

        return total_delivery_fee + packaging_fees
    
    
    def calculate_grand_total(self, user_profile):
        """
        Calculate the grand total amount for the cart.
        """
        total_amount = Decimal(self.total_price)  # Convert total_price to Decimal
        # packaging_fees = Decimal(self.calculate_packaging_fees())  # Convert packaging fees to Decimal
        delivery_fees = Decimal(self.calculate_total_delivery_fee(user_profile))  # Convert delivery fees to Decimal

        grand_total = total_amount + delivery_fees
        return grand_total
    
    def get_overall_delivery_range(self):
        """
        Calculate the overall delivery range for the order based on associated OrderProducts.
        """
        today = datetime.now().date()
        order_products = self.order_products.all()

        if not order_products.exists():
            return None  # No products in the order

        # Initialize min and max dates to extreme values
        overall_min_date = None
        overall_max_date = None

        for product in order_products:
            if product.selected_delivery_option:
                min_date = (product.date_created + timedelta(days=product.selected_delivery_option.min_days)).date()
                max_date = (product.date_created + timedelta(days=product.selected_delivery_option.max_days)).date()

                # Update overall min and max dates
                if overall_min_date is None or min_date < overall_min_date:
                    overall_min_date = min_date
                if overall_max_date is None or max_date > overall_max_date:
                    overall_max_date = max_date

        # If any dates are missing, return None
        if not overall_min_date or not overall_max_date:
            return None

        # If overdue
        if overall_max_date < today:
            return f"Delivery was expected by {overall_max_date.strftime('%d %B %Y')} and is now overdue."

        # Humanize the range
        from_date = "today" if overall_min_date == today else overall_min_date.strftime('%d %B %Y')
        to_date = "today" if overall_max_date == today else overall_max_date.strftime('%d %B %Y')

        if from_date == to_date:
            return f"Delivery expected on {from_date}"  # Single day range
        return f"Delivery expected from {from_date} to {to_date}"
    
    def get_vendor_total(self, vendor):
        """
        Calculate the total amount for a specific vendor in this order.
        """
        order_products = self.order_products.filter(product__vendor=vendor)
        return sum(op.amount for op in order_products)
    
    def get_vendor_delivery_cost(self, vendor):
        """
        Calculate the total delivery cost for a specific vendor in this order.
        """
        order_products = self.order_products.filter(product__vendor=vendor)
        return sum(op.selected_delivery_option.cost for op in order_products if op.selected_delivery_option)

    def get_vendor_delivery_date_range(self, vendor):
        """
        Calculate the delivery date range for a specific vendor in the order.
        """
        from datetime import datetime, timedelta

        min_delivery_date = None
        max_delivery_date = None

        # Filter order products by the vendor
        order_products = self.order_products.filter(product__vendor=vendor)

        for order_product in order_products:
            delivery_option = order_product.selected_delivery_option  # Buyer's selected option
            if not delivery_option:
                # Fall back to seller's default delivery option
                delivery_option = ProductDeliveryOption.objects.filter(
                    product=order_product.product,
                    variant=order_product.variant,
                    default=True,
                ).first()

            if delivery_option:
                delivery_dates = delivery_option.get_delivery_date_range()
                if isinstance(delivery_dates, str):  # Handle same-day cases
                    return delivery_dates
                else:
                    min_date, max_date = delivery_dates
                    if min_delivery_date is None or min_date < min_delivery_date:
                        min_delivery_date = min_date
                    if max_delivery_date is None or max_date > max_delivery_date:
                        max_delivery_date = max_date

        if min_delivery_date and max_delivery_date:
            return f"{min_delivery_date.strftime('%d %B')} to {max_delivery_date.strftime('%d %B')}"
        return "Delivery date unavailable"

class OrderProduct(models.Model):
    order = models.ForeignKey(Order, related_name='order_products', on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    variant = models.ForeignKey(Variants, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField()
    price = models.FloatField()
    amount = models.FloatField()  # Could be calculated as quantity * price
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

    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-date_created',)

    def packaging_fee(self):
        return calculate_packaging_fee(self.product.weight, self.product.volume) * self.quantity

    def save(self, *args, **kwargs):
        self.amount = self.quantity * self.price  # Calculate amount
        super().save(*args, **kwargs)
    
    def get_delivery_range(self):
        if not self.selected_delivery_option:
            return None  # No delivery option selected

        today = datetime.now().date()
        min_delivery_date = (self.date_created + timedelta(days=self.selected_delivery_option.min_days)).date()
        max_delivery_date = (self.date_created + timedelta(days=self.selected_delivery_option.max_days)).date()

        # If the delivery is overdue
        if max_delivery_date < today:
            return f"Delivery was expected by {max_delivery_date.strftime('%d %B %Y')} and is now overdue."

        # Humanize the dates
        from_date = "today" if min_delivery_date == today else min_delivery_date.strftime('%d %B %Y')
        to_date = "today" if max_delivery_date == today else max_delivery_date.strftime('%d %B %Y')

        if from_date == to_date:
            return f"Delivery expected on {from_date}"  # If from and to are the same
        return f"Delivery expected from {from_date} to {to_date}"

    def __str__(self):
        return f"{self.product.title} (Order {self.order.order_number})"
    
    def get_delivery_status(self):
        if self.selected_delivery_option:
            return self.selected_delivery_option.get_delivery_status()
        return "Delivery option unavailable"

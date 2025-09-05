from decimal import Decimal
import requests
from django.conf import settings
from django.utils.crypto import get_random_string
from rest_framework.response import Response
from rest_framework.views import APIView

from userauths.models import User
from address.models import Address
from order.models import Order, OrderProduct, Cart
from .models import Payment


class FlutterwaveCallbackAPIView(APIView):
    def get(self, request):
        # Extract data from query parameters
        status = request.GET.get('status')
        tx_ref = request.GET.get('tx_ref')
        transaction_id = request.GET.get('transaction_id')

        # Check payment status
        if status != "successful":
            return Response({"error": "Payment not successful"}, status=400)

        # Verify transaction with Flutterwave
        headers = {
            "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}"
        }
        verify_url = f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify"
        res = requests.get(verify_url, headers=headers)

        if res.status_code != 200:
            return Response({"error": "Failed to verify transaction"}, status=400)

        data = res.json().get("data")
        if not data or data.get("status") != "successful":
            return Response({"error": "Invalid transaction data"}, status=400)

        email = data["customer"]["email"]
        amount = Decimal(str(data["amount"]))
        flutterwave_ref = tx_ref

        # Retrieve the user
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)

        # Get cart and shipping address
        cart = Cart.objects.filter(user=user).first()
        if not cart or not cart.cart_items.exists():
            return Response({"error": "Cart is empty"}, status=400)

        address = Address.objects.filter(user=user, status=True).first()
        if not address:
            return Response({"error": "No shipping address found"}, status=400)
        
        if Payment.objects.filter(ref=flutterwave_ref, verified=True).exists():
            return Response({"message": "Transaction already processed."}, status=200)


        # Save the payment
        payment = Payment.objects.create(
            user=user,
            ref=flutterwave_ref,
            amount=amount,
            email=email,
            verified=True,
        )

        # Create the order
        order = Order.objects.create(
            user=user,
            total=amount,
            payment_method='flutterwave',
            payment_id=payment.id,
            address=address,
            ip=request.META.get('REMOTE_ADDR', '0.0.0.0'),
            is_ordered=True,
            status='pending'
        )

        # Assign vendors
        unique_vendors = {item.product.vendor for item in cart.cart_items.all() if item.product.vendor}
        order.vendors.set(unique_vendors)

        # Generate a unique order number
        while True:
            order_number = f"INVOICE_NO-{get_random_string(8)}"
            if not Order.objects.filter(order_number=order_number).exists():
                break
        order.order_number = order_number
        order.save()

        # Create OrderProduct items and update stock
        for cart_item in cart.cart_items.all():
            price = cart_item.variant.price if cart_item.variant else cart_item.product.price

            OrderProduct.objects.create(
                order=order,
                product=cart_item.product,
                variant=cart_item.variant,
                quantity=cart_item.quantity,
                price=price,
                amount=price * cart_item.quantity,
                selected_delivery_option=cart_item.delivery_option
            )

            # Update product or variant stock
            if cart_item.variant:
                cart_item.variant.quantity -= cart_item.quantity
                cart_item.variant.save()
            else:
                cart_item.product.total_quantity -= cart_item.quantity
                cart_item.product.save()

            # Remove the cart item
            cart_item.delete()

        return Response({"message": "Payment verified and order created successfully"}, status=200)



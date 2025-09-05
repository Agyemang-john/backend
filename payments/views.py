from django.shortcuts import render, redirect
from .models import Payment, UserWallet
from django.conf import settings

# views.py
# views.py
import requests
from django.shortcuts import render, redirect
from django.urls import reverse
from paypalrestsdk import Payment
import paypalrestsdk
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from order.models import Cart, Order, OrderProduct
from address.models import Address
from django.contrib import messages
from product.models import *
from . models import *
from django.utils import timezone
from django.conf import settings
import datetime

from django.utils.crypto import get_random_string
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import requests
from django.conf import settings
from .models import Payment
from rest_framework.permissions import IsAuthenticated
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .tasks import create_order_task


class VerifyPaymentAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        # Get user's cart
        cart = Cart.objects.filter(user=request.user).first()
        if not cart or not cart.cart_items.exists():
            return Response(
                {"status": "failed", "message": "Cart is empty or does not exist"},
                status=status.HTTP_400_BAD_REQUEST
            )

        address = Address.objects.filter(user=request.user, status=True).first()
        if not address:
            return Response(
                {"status": "failed", "message": "Address not provided"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Verify payment with Paystack
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        }
        response = requests.get(url, headers=headers)
        data = response.json()

        if data["status"] and data["data"]["status"] == "success":
            payment_data = data["data"]

            # Save payment details
            payment, created = Payment.objects.get_or_create(
                user=request.user,
                ref=payment_data["reference"],
                defaults={
                    "verified": True,
                    "amount": payment_data["amount"] / 100,  # Convert from kobo
                    "email": payment_data["customer"]["email"],
                },
            )

            # Create an Order
            order = Order.objects.create(
                user=request.user,
                total=payment_data["amount"] / 100,
                payment_method='paystack',
                payment_id=payment.id,  # Use the payment ID
                status="pending",
                address=address,
                ip=request.META.get('REMOTE_ADDR'),
                is_ordered=True,
            )

            unique_vendors = {cart_item.product.vendor for cart_item in cart.cart_items.all() if cart_item.product.vendor}

            # Assign unique vendors to the Order's ManyToMany field
            order.vendors.set(unique_vendors)

            while True:
                order_number = f"INVOICE_NO-{get_random_string(8)}"  # Random 8-character string
                if not Order.objects.filter(order_number=order_number).exists():
                    break
            order.order_number = order_number
            order.save()

            # Loop through cart items and create OrderProduct
            for cart_item in cart.cart_items.all():
                price = cart_item.variant.price if cart_item.variant else cart_item.product.price

                OrderProduct.objects.create(
                    order=order,
                    product=cart_item.product,
                    variant=cart_item.variant,
                    quantity=cart_item.quantity,
                    price=price,
                    amount=price * cart_item.quantity,
                    selected_delivery_option = cart_item.delivery_option
                )

                # Update product or variant stock
                if cart_item.variant:
                    cart_item.variant.quantity -= cart_item.quantity
                    cart_item.variant.save()
                else:
                    cart_item.product.total_quantity -= cart_item.quantity
                    cart_item.product.save()

                # Delete the cart item
                cart_item.delete()


            return Response(
                {
                    "status": "success",
                    "message": "Payment was successful and order created",
                    "order_id": order.id,
                    "order_number": order.order_number,
                },
                status=status.HTTP_200_OK
            )
        else:
            return Response(
                {"status": "failed", "message": "Payment verification failed"},
                status=status.HTTP_400_BAD_REQUEST
            )


@login_required
def subscribe(request):
    if request.method == 'POST':
        plan_id = request.POST.get('plan_id')
        plan = Plan.objects.get(id=plan_id)
        email = request.user.email

        paystack_fee = int(plan.price * 0.01)

        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        }

        data = {
            "email": email,
            "amount": int(plan.price + paystack_fee * 100),  # Paystack uses kobo, so multiply the amount by 100
            "callback_url": request.build_absolute_uri(reverse('payments:verify')),
            "plan": plan.paystack_plan_id
        }

        response = requests.post('https://api.paystack.co/transaction/initialize', headers=headers, json=data)
        response_data = response.json()

        if response_data.get('status'):
            authorization_url = response_data['data']['authorization_url']
            return redirect(authorization_url)
        else:
            return render(request, 'subscription_error.html', {'error': response_data.get('message', 'An error occurred')})

    plans = Plan.objects.all()
    return render(request, 'subscribe.html', {'plans': plans})


def subscription_verify_payment(reference):
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    response = requests.get(f'https://api.paystack.co/transaction/verify/{reference}', headers=headers)
    return response.json()

@login_required
def paystack_execute_subscription(request):
    reference = request.GET.get('reference')
    payment_data = subscription_verify_payment(reference)
    user = request.user
    vendor = Vendor.objects.get(user=user)

    if payment_data and payment_data['data']['status'] == 'success':
        plan_id = payment_data['data']['metadata']['plan_id']
        plan = Plan.objects.get(id=plan_id)

        subscription = Subscription.objects.create(
            vendor=vendor,
            plan=plan,
            active=True
        )
        return render(request, 'subscription_success.html', {'payment': payment_data})
    else:
        return render(request, 'subscription_error.html', {'error': 'Payment verification failed.'})


@login_required
def create_payment(request):
    if request.method == 'POST':
        rate = request.session.get('rate', 1.0)
        currency = request.session.get('currency', "GHS")
        total_amount = 0

        # Get the cart items for the logged-in user
        cart_items = Cart.objects.filter(user=request.user, added=True)
        if not cart_items:
            messages.error(request, "Your cart is empty.")
            return redirect('order:shopcart')
        
        items = []
        for rs in cart_items:
            price = rs.product.price if rs.product.variant == 'None' else rs.variant.price
            total_amount += price * rs.quantity
            items.append({
                "name": rs.product.title,
                "sku": str(rs.product.id),
                "price": str(price),  # Ensure price is correctly formatted as a string
                "currency": currency,
                "quantity": rs.quantity
            })

        payment = Payment({
            "intent": "sale",
            "payer": {
                "payment_method": "paypal"
            },
            "redirect_urls": {
                "return_url": request.build_absolute_uri(reverse('payments:execute_payment')),
                "cancel_url": request.build_absolute_uri(reverse('payments:payment_cancelled'))
            },
            "transactions": [{
                "item_list": {
                    "items": items
                },
                "amount": {
                    "total": "89.00",
                    "currency": "USD"
                },
                "description": "This is the payment transaction description."
            }]
        })

        if payment.create():
            for link in payment.links:
                if link.rel == "approval_url":
                    approval_url = str(link.href)
                    return redirect(approval_url)
        else:
            return render(request, 'payment_error.html', {'error': payment.error})

    return render(request, 'payment.html')

@login_required
def execute_payment(request):
    payment_id = request.GET.get('paymentId')
    payer_id = request.GET.get('PayerID')

    payment = paypalrestsdk.Payment.find(payment_id)

    if payment.execute({"payer_id": payer_id}):
        # Save the payment and order details in the database
        cart_items = Cart.objects.filter(user=request.user)
        total_amount = 0

        for rs in cart_items:
            if rs.product.variant == 'None':
                total_amount += rs.product.price * rs.quantity
            else:
                total_amount += rs.variant.price * rs.quantity
        
        address = Address.objects.filter(user=request.user, status=True).first()  # Assuming address is already saved
        if not address:
            messages.error(request, "Please add an address to your profile.")
            return redirect('profile_address')

        order = Order.objects.create(
            user=request.user,
            total=total_amount,
            payment_method='paypal',
            payment_id=payment_id,
            status=payment.state,
            address=address,
            ip=request.META.get('REMOTE_ADDR'),
            is_ordered=True
        )

        # Generate order number after saving the order to get the order ID
        order.order_number = "INVOICE_NO-" + str(order.id)
        order.save()

        for cart_item in cart_items:
            if cart_item.product.variant == 'None':
                price = cart_item.product.price
            else:
                price = cart_item.variant.price

            OrderProduct.objects.create(
                order=order,
                user=request.user,
                product=cart_item.product,
                variant=cart_item.variant,
                quantity=cart_item.quantity,
                price=price,
                amount=price * cart_item.quantity
            )
            cart_item.delete()  # Remove item from cart after processing
            #### Reduce the quantity
            if cart_item.product.variant == 'None':
                product = Product.objects.get(id=cart_item.product_id)
                product.total_quantity -= cart_item.quantity
                product.save()
            else:
                variant = Variants.objects.get(id=cart_item.product_id)
                variant.quantity -= cart_item.quantity
                variant.save()

        return render(request, 'payment_success.html', {'payment': payment})
    else:
        return render(request, 'payment_error.html', {'error': payment.error})


@login_required
def payment_cancelled(request):
    return render(request, 'payment_cancelled.html')

########################################################################################


@login_required
def paystack_create_payment(request):
    if request.method == 'POST':
        total_amount = 0
        # Get the cart items for the logged-in user
        cart_items = Cart.objects.filter(user=request.user, added=True)
        if not cart_items:
            messages.error(request, "Your cart is empty.")
            return redirect('order:shopcart')
        
        for rs in cart_items:
            if rs.product.variant == 'None':
                total_amount += rs.product.price * rs.quantity
            else:
                total_amount += rs.variant.price * rs.quantity
        
        email = request.user.email

        headers = {
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        }
        paystack_fee = total_amount * 0.01

        data = {
            "email": email,
            "amount": int((total_amount + paystack_fee) * 100),  # Paystack uses kobo, so multiply the amount by 100
            "callback_url": request.build_absolute_uri(reverse('payments:paystack_execute'))
        }

        response = requests.post('https://api.paystack.co/transaction/initialize', headers=headers, json=data)
        response_data = response.json()

        if response_data.get('status'):
            authorization_url = response_data['data']['authorization_url']
            return redirect(authorization_url)
        else:
            return render(request, 'payment_error.html', {'error': response_data.get('message', 'An error occurred')})

    return render(request, 'payment.html')

@login_required
def paystack_execute(request):
    reference = request.GET.get('reference')
    payment_data = verify_payment(reference)

    if payment_data and payment_data['status'] == 'success':
        # Save the payment and order details in the database
        total_amount = 0
        cart_items = Cart.objects.filter(user=request.user, added=True)

        for rs in cart_items:
            if rs.product.variant == 'None':
                total_amount += rs.product.price * rs.quantity
            else:
                total_amount += rs.variant.price * rs.quantity
        
        address = Address.objects.filter(user=request.user, status=True).first()  # Assuming address is already saved
        if not address:
            messages.error(request, "Please add an address to your profile.")
            return redirect('profile_address')

        order = Order.objects.create(
            user=request.user,
            total=total_amount,
            payment_method='paystack',
            payment_id=payment_data['reference'],
            status=payment_data['status'],
            address=address,
            ip=request.META.get('REMOTE_ADDR'),
            is_ordered=True
        )

        # Generate order number after saving the order to get the order ID
        order.order_number = "INVOICE_NO-" + str(order.id)
        order.save()

        for cart_item in cart_items:
            if cart_item.product.variant == 'None':
                price = cart_item.product.price
            else:
                price = cart_item.variant.price

            OrderProduct.objects.create(
                order=order,
                user=request.user,
                product=cart_item.product,
                variant=cart_item.variant,
                quantity=cart_item.quantity,
                price=price,
                amount=price * cart_item.quantity
            )
            cart_item.delete()  # Remove item from cart after processing

            if cart_item.product.variant == 'None':
                product = Product.objects.get(id=cart_item.product.id)
                product.total_quantity -= cart_item.quantity
                product.save()
            else:
                variant = Variants.objects.get(id=cart_item.product.id)
                variant.quantity -= cart_item.quantity
                variant.save()

        return render(request, 'payment_success.html', {'payment': payment_data})
    else:
        return render(request, 'payment_error.html', {'error': 'Payment verification failed.'})


@login_required
def payment_cancelled(request):
    return render(request, 'payment_cancelled.html')

def verify_payment(reference):
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
    }

    response = requests.get(f'https://api.paystack.co/transaction/verify/{reference}', headers=headers)
    response_data = response.json()

    if response_data['status']:
        return response_data['data']
    else:
        return None

@login_required
def flutter_payment(request):
    if request.method == 'POST':
        rate = request.session.get('rate', 1.0)
        currency = request.session.get('currency', "GHS")
        total_amount = 0

        # Get the cart items for the logged-in user
        cart_items = Cart.objects.filter(user=request.user, added=True)
        if not cart_items:
            messages.error(request, "Your cart is empty.")
            return redirect('order:shopcart')

        # items = []
        # for rs in cart_items:
        #     price = rs.product.price if rs.product.variant == 'None' else rs.variant.price
        #     total_amount += price * rs.quantity
        #     items.append({
        #         "name": rs.product.title,
        #         "sku": str(rs.product.id),
        #         "price": str(price),  # Ensure price is correctly formatted as a string
        #         "currency": currency,
        #         "quantity": rs.quantity
        #     })

        # # Convert total_amount to the required currency using the rate
        # converted_amount = total_amount * rate

        # Prepare payload for Flutterwave
        payload = {
            "tx_ref": f"tx-bhrfgg4r4hrjgdhj4r",
            "amount": "90.00",
            "currency": "GHS",
            "redirect_url": request.build_absolute_uri(reverse('payments:flutter_execute')),
            "payment_options": "card, ussd, mobilemoneyghana",
            "meta": {
                "consumer_id": request.user.id,
                "consumer_mac": "92a3-912ba-1192a"
            },
            "customer": {
                "email": request.user.email,
                "phonenumber": request.user.phone,  # Assuming user profile has phone_number
                "name": f"{request.user.first_name} {request.user.last_name}"
            },
            "customizations": {
                "title": "Payment for Goods",
                "description": "Payment for items in cart",
                "logo": "https://your-logo-url.com/logo.png"  # Replace with your logo URL
            }
        }

        headers = {
            "Authorization": f"Bearer FLWSECK_TEST-16d927f090fbd83a63e74717f2559728-X",
            "Content-Type": "application/json"
        }

        try:
            response = requests.post("https://api.flutterwave.com/v3/payments", json=payload, headers=headers)
            if response.status_code == 200:
                data = response.json()
                link = data['data']['link']
                return redirect(link)
            else:
                messages.error(request, "Error creating payment. Please try again.")
                return render(request, 'payment_error.html', {'error': response})
        except Exception as e:
            messages.error(request, f"An error occurred: {e}")
            return render(request, 'payment_error.html', {'error': response})

    return render(request, 'payment.html')

@login_required
def execute_flutter(request):
    transaction_id = request.GET.get('transaction_id')

    # Verify the payment
    headers = {
        "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    response = requests.get(f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify", headers=headers)
    data = response.json()

    if data['status'] == 'success':
        # Save the payment and order details in the database
        cart_items = Cart.objects.filter(user=request.user)
        total_amount = 0

        for rs in cart_items:
            if rs.product.variant == 'None':
                total_amount += rs.product.price * rs.quantity
            else:
                total_amount += rs.variant.price * rs.quantity

        address = Address.objects.filter(user=request.user, status=True).first()  # Assuming address is already saved
        if not address:
            messages.error(request, "Please add an address to your profile.")
            return redirect('profile_address')

        order = Order.objects.create(
            user=request.user,
            total=total_amount,
            payment_method='flutterwave',
            payment_id=transaction_id,
            status=data['data']['status'],
            address=address,
            ip=request.META.get('REMOTE_ADDR'),
            is_ordered=True
        )

        # Generate order number after saving the order to get the order ID
        order.order_number = "INVOICE_NO-" + str(order.id)
        order.save()

        for cart_item in cart_items:
            if cart_item.product.variant == 'None':
                price = cart_item.product.price
            else:
                price = cart_item.variant.price

            OrderProduct.objects.create(
                order=order,
                user=request.user,
                product=cart_item.product,
                variant=cart_item.variant,
                quantity=cart_item.quantity,
                price=price,
                amount=price * cart_item.quantity
            )
            cart_item.delete()  # Remove item from cart after processing
            #### Reduce the quantity
            if cart_item.product.variant == 'None':
                product = Product.objects.get(id=cart_item.product_id)
                product.total_quantity -= cart_item.quantity
                product.save()
            else:
                variant = Variants.objects.get(id=cart_item.variant_id)
                variant.quantity -= cart_item.quantity
                variant.save()

        return render(request, 'payment_success.html', {'payment': data['data']})
    else:
        return render(request, 'payment_error.html', {'error': data['message']})

@login_required
def payment_cancelled(request):
    return render(request, 'payment_cancelled.html')
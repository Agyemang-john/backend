from .models import Payment
from django.conf import settings
import requests
from order.models import Cart, Order
from address.models import Address
from product.models import *
from . models import *
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from .tasks import create_order_from_payment_task


class VerifyPaymentAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        user = request.user

        # Basic validations
        cart = Cart.objects.filter(user=user).first()
        if not cart or not cart.cart_items.exists():
            return Response(
                {"status": "failed", "message": "Cart is empty or does not exist"},
                status=status.HTTP_400_BAD_REQUEST
            )

        address = Address.objects.filter(user=user, status=True).first()
        if not address:
            return Response(
                {"status": "failed", "message": "No active address found"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Verify payment with Paystack
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        headers = {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logger.error(f"Paystack verification failed for ref {reference}: {e}")
            return Response(
                {"status": "failed", "message": "Payment verification failed"},
                status=status.HTTP_502_BAD_GATEWAY
            )

        if not data.get("status") or data["data"]["status"] != "success":
            return Response(
                {"status": "failed", "message": "Payment not successful"},
                status=status.HTTP_400_BAD_REQUEST
            )

        payment_data = data["data"]

        # Prevent duplicate processing (idempotency)
        if Payment.objects.filter(ref=payment_data["reference"]).exists():
            # Already processed — find existing order
            payment = Payment.objects.get(ref=payment_data["reference"])
            try:
                order = Order.objects.get(payment_id=payment.id, user=user)
                return Response({
                    "status": "success",
                    "message": "Payment already processed",
                    "order_id": order.id,
                    "order_number": order.order_number,
                })
            except Order.DoesNotExist:
                pass  # Continue to recreate if needed

        # Create or get Payment object (safe to do sync)
        payment, created = Payment.objects.get_or_create(
            user=user,
            ref=payment_data["reference"],
            defaults={
                "verified": True,
                "amount": payment_data["amount"] / 100,
                "email": payment_data["customer"]["email"],
            }
        )

        if not created:
            payment.verified = True
            payment.amount = payment_data["amount"] / 100
            payment.save()

        # Serialize cart items for Celery (avoid passing queryset)
        cart_items_data = []
        for item in cart.cart_items.select_related('product', 'variant').all():
            cart_items_data.append({
                "product_id": item.product.id,
                "variant_id": item.variant.id if item.variant else None,
                "quantity": item.quantity,
                "delivery_option_id": item.delivery_option.id if item.delivery_option else None,
            })

        # Offload heavy work to Celery
        create_order_from_payment_task.delay(
            user_id=user.id,
            payment_data=payment_data,
            payment_id=payment.id,
            cart_items_data=cart_items_data,
            address_id=address.id,
            ip=request.META.get('REMOTE_ADDR'),
            reference=reference,
        )

        # Immediate success response to user
        return Response({
            "status": "success",
            "message": "Payment verified successfully. Your order is being processed...",
            "reference": reference,
        }, status=status.HTTP_200_OK)

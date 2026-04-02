"""
payments/views.py
API views for payment processing and subscription management:
- VerifyPaymentAPIView: verifies Paystack payment and triggers order creation
- SubscriptionPlanListView: lists available subscription plans
- InitiateSubscriptionView / VerifySubscriptionView: subscription payment flow
- CurrentSubscriptionView / CancelSubscriptionView / AutoRenewToggleView: manage subscriptions
- PaymentHistoryView / SavedCardsView: transaction and card management
- PaystackWebhookView: receives Paystack webhook events
"""

import logging
from django.shortcuts import redirect
from .models import Payment
from django.conf import settings
import requests
from order.models import Cart, Order
from address.models import Address
from product.models import *
from .models import *
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from .tasks import create_order_from_payment_task

logger = logging.getLogger(__name__)


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
    
# subscriptions/views.py
"""
Thin views. All logic lives in services.py.
Views only handle: auth, serializer validation, calling service, returning response.
"""

import logging
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from .serializers import (
    SubscriptionPlanSerializer,
    VendorSubscriptionSerializer,
    ActiveSubscriptionSerializer,
    SubscriptionUsageSerializer,
    PaymentTransactionSerializer,
    PaystackAuthorizationSerializer,
    InitiateSubscriptionSerializer,
    CancelSubscriptionSerializer,
    UpdateAutoRenewSerializer,
)
from .models import (
    SubscriptionPlan,
    VendorSubscription,
    SubscriptionUsage,
    PaymentTransaction,
    PaystackAuthorization,
)
from . import services

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Plan listing — public endpoint
# ─────────────────────────────────────────────────────────────────────────────

class SubscriptionPlanListView(APIView):
    """
    GET /api/v1/payments/plans/
    Returns all active plans + the vendor's current subscription in one request.
    - Plans list is public (AllowAny)
    - current_subscription is only populated when the request is authenticated
      (anonymous vendors get null — no error)
    """
    permission_classes = [AllowAny]

    def get(self, request):
        plans = services.get_active_plans()
        plans_data = SubscriptionPlanSerializer(plans, many=True).data

        # Populate current_subscription when authenticated
        current_subscription = None
        if request.user and request.user.is_authenticated:
            vendor = getattr(request.user, 'vendor_user', None)
            if vendor:
                active_sub = VendorSubscription.objects.filter(
                    vendor=vendor, status='active'
                ).select_related('plan').first()
                if active_sub:
                    current_subscription = ActiveSubscriptionSerializer(active_sub).data

        return Response({
            "plans": plans_data,
            "current_subscription": current_subscription,
        })


# ─────────────────────────────────────────────────────────────────────────────
# 2. Initiate subscription — vendor clicks "Subscribe now"
# ─────────────────────────────────────────────────────────────────────────────

class InitiateSubscriptionView(APIView):
    """
    POST /api/subscriptions/initiate/
    Body: { plan_id: int, billing: "monthly" | "yearly" }
    Returns: { authorization_url, reference }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = InitiateSubscriptionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        vendor = getattr(request.user, 'vendor_user', None)
        if not vendor:
            return Response(
                {"error": "User has no associated vendor account."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            result = services.initiate_subscription(
                vendor=vendor,
                plan_id=serializer.validated_data["plan_id"],
                billing=serializer.validated_data["billing"],
            )
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"InitiateSubscription error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Verify payment — called after Paystack redirect
# ─────────────────────────────────────────────────────────────────────────────

class VerifySubscriptionView(APIView):
    """
    GET /api/subscriptions/verify/?ref=SUB-1-XXXXXXXX
    Called when Paystack redirects back to the frontend.
    The frontend hits this to confirm activation before showing the success modal.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        reference = request.query_params.get("ref")
        if not reference:
            return Response(
                {"error": "Missing reference parameter."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = services.verify_and_activate(reference)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"VerifySubscription error ref={reference}: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Current subscription status — vendor dashboard
# ─────────────────────────────────────────────────────────────────────────────

class CurrentSubscriptionView(APIView):
    """
    GET /api/subscriptions/current/
    Returns the vendor's active subscription + usage.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        vendor = getattr(request.user, 'vendor_user', None)
        if not vendor:
            return Response(
                {"error": "No vendor account found."},
                status=status.HTTP_403_FORBIDDEN,
            )

        active_sub = VendorSubscription.objects.filter(
            vendor=vendor,
            status__in=['active', 'trial'],
        ).select_related('plan').first()

        usage = getattr(vendor, 'subscription_usage', None)

        return Response({
            "subscription": VendorSubscriptionSerializer(active_sub).data if active_sub else None,
            "usage": SubscriptionUsageSerializer(usage).data if usage else None,
        })


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cancel subscription
# ─────────────────────────────────────────────────────────────────────────────

class CancelSubscriptionView(APIView):
    """
    POST /api/subscriptions/cancel/
    Body: { reason: string (optional) }
    Cancels auto-renewal; access continues until end_date.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CancelSubscriptionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        vendor = getattr(request.user, 'vendor_user', None)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        try:
            sub = services.cancel_subscription(
                vendor=vendor,
                reason=serializer.validated_data.get("reason", ""),
            )
            return Response(
                {
                    "message": "Subscription cancelled. Access continues until end of billing period.",
                    "access_until": sub.end_date,
                    "subscription": ActiveSubscriptionSerializer(sub).data,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Toggle auto-renew
# ─────────────────────────────────────────────────────────────────────────────

class AutoRenewToggleView(APIView):
    """
    PATCH /api/subscriptions/auto-renew/
    Body: { auto_renew: bool }
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        serializer = UpdateAutoRenewSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        vendor = getattr(request.user, 'vendor_user', None)
        sub = VendorSubscription.objects.filter(
            vendor=vendor, status='active'
        ).first()

        if not sub:
            return Response({"error": "No active subscription."}, status=status.HTTP_404_NOT_FOUND)

        sub.auto_renew = serializer.validated_data["auto_renew"]
        sub.save(update_fields=["auto_renew"])

        return Response({
            "auto_renew": sub.auto_renew,
            "message": "Auto-renewal updated.",
        })


# ─────────────────────────────────────────────────────────────────────────────
# 7. Payment history
# ─────────────────────────────────────────────────────────────────────────────

class PaymentHistoryView(APIView):
    """
    GET /api/subscriptions/payments/
    Returns paginated transaction history for the vendor.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        vendor = getattr(request.user, 'vendor_user', None)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        transactions = PaymentTransaction.objects.filter(
            vendor=vendor
        ).select_related('subscription__plan').order_by('-created_at')[:50]

        return Response(PaymentTransactionSerializer(transactions, many=True).data)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Saved cards
# ─────────────────────────────────────────────────────────────────────────────

class SavedCardsView(APIView):
    """
    GET  /api/subscriptions/cards/
    DELETE /api/subscriptions/cards/{id}/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        vendor = getattr(request.user, 'vendor_user', None)
        cards = PaystackAuthorization.objects.filter(
            vendor=vendor, is_reusable=True
        ).order_by('-is_default', '-created_at')
        return Response(PaystackAuthorizationSerializer(cards, many=True).data)


class SavedCardDetailView(APIView):
    """DELETE /api/subscriptions/cards/{id}/"""
    permission_classes = [IsAuthenticated]

    def delete(self, request, card_id):
        vendor = getattr(request.user, 'vendor_user', None)
        card = PaystackAuthorization.objects.filter(
            pk=card_id, vendor=vendor
        ).first()

        if not card:
            return Response({"error": "Card not found."}, status=status.HTTP_404_NOT_FOUND)

        was_default = card.is_default
        card.delete()

        # If we deleted the default card, promote the next one
        if was_default:
            next_card = PaystackAuthorization.objects.filter(
                vendor=vendor, is_reusable=True
            ).first()
            if next_card:
                next_card.is_default = True
                next_card.save(update_fields=["is_default"])

        return Response({"message": "Card removed."}, status=status.HTTP_204_NO_CONTENT)


class SetDefaultCardView(APIView):
    """PATCH /api/subscriptions/cards/{id}/set-default/"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, card_id):
        vendor = getattr(request.user, 'vendor_user', None)
        card = PaystackAuthorization.objects.filter(
            pk=card_id, vendor=vendor
        ).first()

        if not card:
            return Response({"error": "Card not found."}, status=status.HTTP_404_NOT_FOUND)

        PaystackAuthorization.objects.filter(vendor=vendor).update(is_default=False)
        card.is_default = True
        card.save(update_fields=["is_default"])

        return Response({"message": "Default card updated."})


# ─────────────────────────────────────────────────────────────────────────────
# 9. Paystack Webhook — no auth, signature verification in service
# ─────────────────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name='dispatch')
class PaystackWebhookView(APIView):
    """
    POST /api/webhooks/paystack/
    Paystack calls this endpoint for every payment event.
    IMPORTANT: Must be excluded from JWT auth middleware.
    """
    permission_classes = [AllowAny]
    authentication_classes = []     # No JWT — this is Paystack calling us

    def post(self, request):
        signature = request.headers.get("x-paystack-signature", "")
        raw_body = request.body

        try:
            result = services.handle_paystack_webhook(
                payload=request.data,
                signature=signature,
                raw_body=raw_body,
            )
            logger.info(f"Webhook handled: {result}")
            return Response({"status": "ok"}, status=status.HTTP_200_OK)

        except PermissionError as e:
            logger.warning(f"Webhook signature invalid: {e}")
            return Response({"error": "Invalid signature."}, status=status.HTTP_403_FORBIDDEN)

        except Exception as e:
            logger.error(f"Webhook processing error: {e}")
            # Always return 200 to Paystack even on errors
            # Otherwise Paystack will retry indefinitely
            return Response({"status": "ok"}, status=status.HTTP_200_OK)

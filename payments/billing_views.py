# subscriptions/billing_views.py
# Billing dashboard views — plan management, payment methods, history, overview.
#
# Add to subscriptions/urls.py:
#   path('billing/overview/',       BillingOverviewView.as_view(),      name='billing-overview'),
#   path('billing/history/',        BillingHistoryView.as_view(),       name='billing-history'),
#   path('billing/cards/',          BillingCardsView.as_view(),         name='billing-cards'),
#   path('billing/cards/<int:pk>/default/', SetDefaultCardView.as_view(), name='billing-card-default'),
#   path('billing/cards/<int:pk>/', DeleteCardView.as_view(),           name='billing-card-delete'),
#   path('billing/pay-now/',        ManualPaymentView.as_view(),        name='billing-pay-now'),

import logging
from django.utils import timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .models import (
    VendorSubscription, SubscriptionUsage,
    PaymentTransaction, PaystackAuthorization, SubscriptionPlan,
)
from .billing_serializers import (
    BillingOverviewSerializer,
    BillingSubscriptionSerializer,
    PaymentTransactionSerializer,
    SavedCardSerializer,
    BillingPlanSerializer,
)
from . import services

logger = logging.getLogger(__name__)


def _get_vendor(request):
    vendor = getattr(request.user, 'vendor_user', None)
    return vendor


def _active_sub(vendor):
    return (
        VendorSubscription.objects
        .filter(vendor=vendor, status__in=['active', 'trial'])
        .select_related('plan')
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Overview — single endpoint for the entire billing dashboard
# ─────────────────────────────────────────────────────────────────────────────

class BillingOverviewView(APIView):
    """
    GET /api/v1/payments/billing/overview/
    Returns subscription, usage, recent transactions, saved cards, and available plans.
    The frontend calls this once on mount and populates all four tabs from it.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        vendor = _get_vendor(request)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        sub   = _active_sub(vendor)
        usage = getattr(vendor, 'subscription_usage', None)

        recent_txns = PaymentTransaction.objects.filter(
            vendor=vendor
        ).select_related('subscription__plan', 'authorization').order_by('-created_at')[:5]

        cards = PaystackAuthorization.objects.filter(
            vendor=vendor, is_reusable=True
        ).order_by('-is_default', '-created_at')

        plans = SubscriptionPlan.objects.filter(is_active=True).order_by('price')

        data = {
            "subscription":        sub,
            "usage":               usage,
            "recent_transactions": recent_txns,
            "saved_cards":         cards,
            "available_plans":     plans,
        }
        serializer = BillingOverviewSerializer(data)
        return Response(serializer.data)


# ─────────────────────────────────────────────────────────────────────────────
# Payment history — paginated full list
# ─────────────────────────────────────────────────────────────────────────────

class BillingHistoryView(APIView):
    """
    GET /api/v1/payments/billing/history/?page=1&page_size=20
    Returns paginated payment history.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        vendor = _get_vendor(request)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        page_size = min(int(request.query_params.get('page_size', 20)), 100)
        page      = max(int(request.query_params.get('page', 1)), 1)
        offset    = (page - 1) * page_size

        qs = PaymentTransaction.objects.filter(vendor=vendor).select_related(
            'subscription__plan', 'authorization'
        ).order_by('-created_at')

        total = qs.count()
        txns  = qs[offset:offset + page_size]

        return Response({
            "results":    PaymentTransactionSerializer(txns, many=True).data,
            "total":      total,
            "page":       page,
            "page_size":  page_size,
            "total_pages": max(1, -(-total // page_size)),  # ceiling division
        })


# ─────────────────────────────────────────────────────────────────────────────
# Saved cards management
# ─────────────────────────────────────────────────────────────────────────────

class BillingCardsView(APIView):
    """GET /api/v1/payments/billing/cards/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        vendor = _get_vendor(request)
        cards  = PaystackAuthorization.objects.filter(
            vendor=vendor, is_reusable=True
        ).order_by('-is_default', '-created_at')
        return Response(SavedCardSerializer(cards, many=True).data)


class SetDefaultCardView(APIView):
    """PATCH /api/v1/payments/billing/cards/<pk>/default/"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        vendor = _get_vendor(request)
        card = PaystackAuthorization.objects.filter(pk=pk, vendor=vendor).first()
        if not card:
            return Response({"error": "Card not found."}, status=status.HTTP_404_NOT_FOUND)

        # Clear all defaults then set this one
        PaystackAuthorization.objects.filter(vendor=vendor).update(is_default=False)
        card.is_default = True
        card.save(update_fields=['is_default'])

        return Response(SavedCardSerializer(card).data)


class DeleteCardView(APIView):
    """DELETE /api/v1/payments/billing/cards/<pk>/"""
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        vendor = _get_vendor(request)
        card = PaystackAuthorization.objects.filter(pk=pk, vendor=vendor).first()
        if not card:
            return Response({"error": "Card not found."}, status=status.HTTP_404_NOT_FOUND)

        was_default = card.is_default
        card.delete()

        # Promote next card to default if we deleted the default
        if was_default:
            next_card = PaystackAuthorization.objects.filter(
                vendor=vendor, is_reusable=True
            ).first()
            if next_card:
                next_card.is_default = True
                next_card.save(update_fields=['is_default'])

        return Response({"message": "Card removed."}, status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────────────────────
# Manual payment — "Pay now" button
# ─────────────────────────────────────────────────────────────────────────────

class ManualPaymentView(APIView):
    """
    POST /api/v1/payments/billing/pay-now/
    Charges the vendor's default card immediately.
    Used when a subscription is past_due and the vendor wants to catch up.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        vendor = _get_vendor(request)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        sub = VendorSubscription.objects.filter(
            vendor=vendor, status__in=['past_due', 'active']
        ).select_related('plan').first()

        if not sub:
            return Response({"error": "No active subscription to pay for."}, status=status.HTTP_404_NOT_FOUND)

        default_card = PaystackAuthorization.objects.filter(
            vendor=vendor, is_default=True, is_reusable=True
        ).first()

        if not default_card:
            return Response({
                "error": "no_default_card",
                "detail": "No default payment card on file. Please add a card first.",
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = services.charge_for_renewal(sub, default_card)
            return Response({
                "message": "Payment successful.",
                "reference": result.get("reference"),
                "amount": str(sub.plan.price),
            })
        except Exception as e:
            logger.error(f"ManualPayment error vendor={vendor.id}: {e}")
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ─────────────────────────────────────────────────────────────────────────────
# Add a new card via Paystack checkout
# ─────────────────────────────────────────────────────────────────────────────

class AddCardView(APIView):
    """
    POST /api/v1/payments/billing/add-card/
    Returns a Paystack authorization URL.
    The vendor completes a GHS 1.00 charge (refunded) to tokenize the card.
    On callback, the authorization is stored and the GHS 1.00 is refunded.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        vendor = _get_vendor(request)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        try:
            result = services.initiate_card_add(vendor)
            return Response(result)
        except Exception as e:
            logger.error(f"AddCard error: {e}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class VerifyCardAddView(APIView):
    """
    GET /api/v1/payments/billing/verify-card/?ref=CARD-ADD-xxx
 
    Called when Paystack redirects the vendor back after the GHS 1 card
    tokenization charge. Saves the card and refunds the charge.
 
    The frontend (billing/cards page) calls this in a useEffect when it
    detects ?card_ref= in the URL, then shows a success toast.
    """
    permission_classes = [IsAuthenticated]
 
    def get(self, request):
        reference = request.query_params.get("ref")
        if not reference:
            return Response(
                {"error": "Missing ref parameter."},
                status=status.HTTP_400_BAD_REQUEST,
            )
 
        if not reference.startswith("CARD-ADD-"):
            return Response(
                {"error": "Invalid reference format."},
                status=status.HTTP_400_BAD_REQUEST,
            )
 
        try:
            result = services.verify_card_add(reference)
            return Response(result)
        except Exception as exc:
            logger.error(f"VerifyCardAdd error ref={reference}: {exc}")
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )    

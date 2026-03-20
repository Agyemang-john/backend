# subscriptions/momo_views.py

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

# ⚠️  Import from .models, NOT .momo_models
from .momo_models import MomoAccount, BillingProfile
from .momo_serializers import (
    MomoAccountSerializer,
    BillingProfileSerializer,
    InitiateMomoSerializer,
    ManualMomoSerializer,
)
from . import momo_services

logger = logging.getLogger(__name__)


def _vendor(request):
    return getattr(request.user, 'vendor_user', None)


# ─────────────────────────────────────────────────────────────────────────────
# Billing Profile
# ─────────────────────────────────────────────────────────────────────────────

class BillingProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        vendor = _vendor(request)
        if not vendor:
            return Response({'error': 'No vendor account.'}, status=status.HTTP_403_FORBIDDEN)
        return Response(BillingProfileSerializer(
            momo_services.get_or_create_billing_profile(vendor)
        ).data)

    def put(self, request):
        return self._save(request, partial=False)

    def patch(self, request):
        return self._save(request, partial=True)

    def _save(self, request, partial):
        vendor = _vendor(request)
        if not vendor:
            return Response({'error': 'No vendor account.'}, status=status.HTTP_403_FORBIDDEN)
        profile    = momo_services.get_or_create_billing_profile(vendor)
        serializer = BillingProfileSerializer(profile, data=request.data, partial=partial)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)


# ─────────────────────────────────────────────────────────────────────────────
# MoMo Accounts
# ─────────────────────────────────────────────────────────────────────────────

class MomoAccountListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        vendor = _vendor(request)
        return Response(MomoAccountSerializer(
            MomoAccount.objects.filter(vendor=vendor).order_by('-is_default', '-created_at'),
            many=True,
        ).data)

    def post(self, request):
        vendor = _vendor(request)
        if not vendor:
            return Response({'error': 'No vendor account.'}, status=status.HTTP_403_FORBIDDEN)

        phone = request.data.get('phone', '').replace(' ', '').replace('-', '')
        if phone.startswith('0') and len(phone) == 10:
            phone = '+233' + phone[1:]
        elif not phone.startswith('+'):
            phone = '+' + phone

        if MomoAccount.objects.filter(vendor=vendor, phone=phone).exists():
            return Response({'error': 'This number is already saved.'}, status=status.HTTP_400_BAD_REQUEST)

        serializer = MomoAccountSerializer(data={**request.data, 'phone': phone})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        is_first = not MomoAccount.objects.filter(vendor=vendor).exists()
        account  = serializer.save(vendor=vendor, is_default=is_first)
        return Response(MomoAccountSerializer(account).data, status=status.HTTP_201_CREATED)


class MomoAccountDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        vendor  = _vendor(request)
        account = MomoAccount.objects.filter(pk=pk, vendor=vendor).first()
        if not account:
            return Response({'error': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

        was_default = account.is_default
        account.delete()
        if was_default:
            nxt = MomoAccount.objects.filter(vendor=vendor).first()
            if nxt:
                nxt.is_default = True
                nxt.save(update_fields=['is_default'])

        return Response({'message': 'MoMo account removed.'}, status=status.HTTP_204_NO_CONTENT)


class SetDefaultMomoView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        vendor  = _vendor(request)
        account = MomoAccount.objects.filter(pk=pk, vendor=vendor).first()
        if not account:
            return Response({'error': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)
        MomoAccount.objects.filter(vendor=vendor).update(is_default=False)
        account.is_default = True
        account.save(update_fields=['is_default'])
        return Response(MomoAccountSerializer(account).data)


# ─────────────────────────────────────────────────────────────────────────────
# Initiate MoMo subscription payment
# ─────────────────────────────────────────────────────────────────────────────

class InitiateMomoView(APIView):
    """
    POST /api/v1/payments/momo/initiate/

    Returns one of:
      200 { reference, status:'pending', display_text, ... }   → show polling UI
      400 { error:'billing_profile_incomplete', detail:... }   → show form
      400 { error:'pending_ussd_session', detail:... }         → show "check phone" UI
      400 { error:'...' }                                       → generic error
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = InitiateMomoSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        vendor = _vendor(request)
        if not vendor:
            return Response({'error': 'No vendor account.'}, status=status.HTTP_403_FORBIDDEN)

        try:
            result = momo_services.initiate_momo_charge(
                vendor   = vendor,
                plan_id  = serializer.validated_data['plan_id'],
                billing  = serializer.validated_data['billing'],
                phone    = serializer.validated_data['phone'],
                provider = serializer.validated_data['provider'],
                save     = serializer.validated_data.get('save', False),
            )
            return Response(result, status=status.HTTP_200_OK)

        except ValueError as exc:
            msg = str(exc)

            # Billing details incomplete
            if msg.startswith('billing_profile_incomplete:'):
                return Response({
                    'error':  'billing_profile_incomplete',
                    'detail': msg.split(':', 1)[1].strip(),
                }, status=status.HTTP_400_BAD_REQUEST)

            # Open USSD session on phone — vendor needs to check their phone
            if msg.startswith('pending_ussd_session:'):
                return Response({
                    'error':  'pending_ussd_session',
                    'detail': msg.split(':', 1)[1].strip(),
                }, status=status.HTTP_400_BAD_REQUEST)

            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

        except Exception as exc:
            logger.error(f'InitiateMoMo error vendor={vendor.id}: {exc}', exc_info=True)
            return Response(
                {'error': 'An unexpected error occurred. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Poll MoMo status
# ─────────────────────────────────────────────────────────────────────────────

class PollMomoStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        reference = request.query_params.get('ref')
        if not reference:
            return Response({'error': 'Missing ref parameter.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(momo_services.poll_momo_status(reference))
        except Exception as exc:
            logger.error(f'PollMoMo error ref={reference}: {exc}')
            # Non-500 so frontend keeps polling through transient errors
            return Response({
                'reference': reference, 'status': 'pending',
                'message': 'Checking payment status…', 'activated': False,
            })

class SubmitMomoOtpView(APIView):
    """
    POST /api/v1/payments/momo/submit-otp/
    Body: { reference: str, otp: str }
    
    Called when the vendor receives an SMS OTP and types it into the UI.
    Returns { status: 'pending'|'success'|'failed', message: str }
    After this, the frontend continues polling GET /momo/status/?ref=
    """
    permission_classes = [IsAuthenticated]
 
    def post(self, request):
        reference = request.data.get('reference')
        otp       = request.data.get('otp', '').strip()
 
        if not reference:
            return Response({'error': 'Missing reference.'}, status=status.HTTP_400_BAD_REQUEST)
        if not otp or not otp.isdigit():
            return Response({'error': 'Please enter the numeric OTP from the SMS.'}, status=status.HTTP_400_BAD_REQUEST)
 
        try:
            result = momo_services.submit_momo_otp(reference, otp)
            return Response(result)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.error(f'SubmitOTP error ref={reference}: {exc}', exc_info=True)
            return Response({'error': 'OTP submission failed. Please try again.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# ─────────────────────────────────────────────────────────────────────────────
# Manual MoMo payment — "Pay now"
# ─────────────────────────────────────────────────────────────────────────────

class ManualMomoPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ManualMomoSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        vendor = _vendor(request)
        if not vendor:
            return Response({'error': 'No vendor account.'}, status=status.HTTP_403_FORBIDDEN)

        try:
            result = momo_services.initiate_momo_manual_payment(
                vendor   = vendor,
                momo_id  = serializer.validated_data.get('momo_id'),
                phone    = serializer.validated_data.get('phone'),
                provider = serializer.validated_data.get('provider'),
            )
            return Response(result)
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith('pending_ussd_session:'):
                return Response({'error': 'pending_ussd_session', 'detail': msg.split(':', 1)[1].strip()}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.error(f'ManualMoMo error vendor={vendor.id}: {exc}', exc_info=True)
            return Response({'error': 'An unexpected error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from vendor.access import get_membership, signed_in_as_seller

from . import services


class SellerVerificationView(APIView):
    """
    The store owner's verification status. Works before approval, when the
    seller cookie isn't issued yet, so it accepts the owner's account sign-in
    (like vendor/my-status). Once the store is approved, changes need the
    seller sign-in (password + code). PUT multipart: id_type, id_number,
    business_reg_number and/or document files.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _vendor(self, request):
        membership = get_membership(request.user)
        if membership is None or membership.role != 'owner':
            return None
        return membership.vendor

    def get(self, request):
        vendor = self._vendor(request)
        if vendor is None:
            return Response({'detail': 'Only the store owner can see verification.'}, status=status.HTTP_403_FORBIDDEN)
        return Response(services.overview(vendor))

    def put(self, request):
        vendor = self._vendor(request)
        if vendor is None:
            return Response({'detail': 'Only the store owner can update verification.'}, status=status.HTTP_403_FORBIDDEN)
        if vendor.is_approved and not signed_in_as_seller(request.user):
            return Response({'detail': 'Sign in to the seller dashboard to change verified details.'},
                            status=status.HTTP_403_FORBIDDEN)
        files = {f: request.FILES[f] for f in services.DOC_LABELS if f in request.FILES}
        try:
            services.submit(
                vendor, request.user,
                id_type=request.data.get('id_type') or None,
                id_number=request.data.get('id_number') if 'id_number' in request.data else None,
                business_reg_number=request.data.get('business_reg_number') if 'business_reg_number' in request.data else None,
                files=files,
            )
        except services.KycError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - upload validation (vendor/kyc.py) raises ValidationError
            from django.core.exceptions import ValidationError
            from rest_framework.exceptions import ValidationError as DRFValidationError
            if isinstance(exc, (ValidationError, DRFValidationError)):
                return Response({'detail': '; '.join(getattr(exc, 'messages', None) or [str(exc)])},
                                status=status.HTTP_400_BAD_REQUEST)
            raise
        return Response(services.overview(vendor))

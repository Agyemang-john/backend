from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from vendor.access import Capability, get_current_vendor, require_capability

from .models import OrderRisk


class SellerOrderHoldView(APIView):
    """Is this order held by the fraud check? (seller order page banner). Never reveals the score or reasons."""
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_ORDERS)]

    def get(self, request, order_id):
        vendor = get_current_vendor(request.user)
        held = OrderRisk.objects.filter(order_id=order_id, status=OrderRisk.HELD,
                                        order__order_products__product__vendor=vendor).exists()
        return Response({'held': held})

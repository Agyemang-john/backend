from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from order.models import Order
from vendor.access import Capability, get_current_vendor, require_capability

from . import services
from .pdf import render


def _pdf_response(invoice, download):
    resp = HttpResponse(render(invoice), content_type='application/pdf')
    disposition = 'attachment' if download else 'inline'
    resp['Content-Disposition'] = f'{disposition}; filename="{invoice.number}.pdf"'
    resp['Cache-Control'] = 'private, max-age=300'
    return resp


class OrderReceiptView(APIView):
    """The shopper's receipt for one of their orders (?download=1 to save)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        order = get_object_or_404(Order.objects.select_related('address', 'user'),
                                  pk=order_id, user=request.user, is_ordered=True)
        invoice = services.issue(order)
        if invoice is None:
            raise Http404
        return _pdf_response(invoice, request.query_params.get('download') == '1')


class SellerOrderInvoiceView(APIView):
    """A store's invoice for its part of an order."""
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_ORDERS, read=Capability.MANAGE_ORDERS)]

    def get(self, request, order_id):
        vendor = get_current_vendor(request.user)
        order = get_object_or_404(Order.objects.select_related('address', 'user'),
                                  pk=order_id, is_ordered=True, order_products__product__vendor=vendor)
        invoice = services.issue(order, vendor)
        if invoice is None:
            raise Http404
        return _pdf_response(invoice, request.query_params.get('download') == '1')

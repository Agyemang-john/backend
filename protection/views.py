import secrets

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from order.models import OrderProduct
from vendor.access import Capability, get_current_vendor, require_capability

from . import services
from .models import Claim, ClaimMessage


class Paging(PageNumberPagination):
    page_size = 20


def _image(upload):
    """Re-encode an evidence photo (drops EXIF/location) or raise ClaimError."""
    if not upload:
        return None
    from django.core.files.base import ContentFile

    from product.review_media import MediaError, process_image
    try:
        full, _thumb, _size = process_image(upload)
    except MediaError as exc:
        raise services.ClaimError(str(exc))
    return ContentFile(full.read() if hasattr(full, 'read') else full, name=f"{secrets.token_hex(8)}.jpg")


def _claim_json(c, viewer, with_messages=False):
    line = c.order_product
    p = line.product
    try:
        image = p.image.url if p and p.image else ''
    except ValueError:
        image = ''
    data = {
        'reference': c.reference, 'status': c.status, 'status_label': c.get_status_display(),
        'reason': c.reason, 'reason_label': c.get_reason_display(),
        'amount_claimed': c.amount_claimed, 'seller_offer_amount': c.seller_offer_amount,
        'resolution_amount': c.resolution_amount, 'resolution_note': c.resolution_note,
        'seller_respond_by': c.seller_respond_by, 'created_at': c.created_at, 'resolved_at': c.resolved_at,
        'refund_sent_at': c.refund_sent_at,
        'order': {'id': c.order_id, 'number': c.order.order_number},
        'item': {'title': p.title if p else 'Item', 'image': image, 'quantity': line.quantity, 'amount': line.amount},
        'seller': c.vendor.name if c.vendor else '',
    }
    if with_messages:
        msgs = []
        for m in c.messages.all():
            try:
                img = m.image.url if m.image else ''
            except ValueError:
                img = ''
            msgs.append({'side': m.side, 'text': m.text, 'image': img, 'created_at': m.created_at,
                         'mine': m.author_id == viewer.pk})
        data['messages'] = msgs
    return data


def _err(exc):
    return Response({'detail': str(exc)}, status=exc.status)


def _base():
    return Claim.objects.select_related('order', 'order_product__product', 'vendor')


# ── Shopper ──────────────────────────────────────────────────────────────────

class EligibilityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, line_id):
        line = get_object_or_404(OrderProduct.objects.select_related('order', 'product'), pk=line_id,
                                 order__user=request.user)
        check = services.eligibility(line, request.user)
        return Response({'can_claim': check.ok, 'message': check.message,
                         'reasons': [{'value': r, 'label': dict(Claim.REASON_CHOICES)[r]} for r in check.reasons],
                         'max_amount': line.amount})


class MyClaimsView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        qs = _base().filter(buyer=request.user)
        pager = Paging()
        page = pager.paginate_queryset(qs, request, view=self)
        return pager.get_paginated_response([_claim_json(c, request.user) for c in page])

    def post(self, request):
        line = get_object_or_404(OrderProduct.objects.select_related('order', 'product__vendor'),
                                 pk=request.data.get('order_product') or 0, order__user=request.user)
        try:
            claim = services.open_claim(request.user, line, request.data.get('reason'), request.data.get('details'),
                                        request.data.get('amount') or None)
            photo = _image(request.FILES.get('image'))
            if photo:
                services.add_message(claim, ClaimMessage.BUYER, request.user, 'Photo', image=photo)
        except services.ClaimError as exc:
            return _err(exc)
        return Response(_claim_json(claim, request.user, True), status=status.HTTP_201_CREATED)


class MyClaimView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request, ref):
        claim = get_object_or_404(_base().prefetch_related('messages'), reference=ref, buyer=request.user)
        return Response(_claim_json(claim, request.user, True))

    def post(self, request, ref, action=None):
        claim = get_object_or_404(_base(), reference=ref, buyer=request.user)
        try:
            if action == 'messages':
                services.add_message(claim, ClaimMessage.BUYER, request.user, request.data.get('text'),
                                     image=_image(request.FILES.get('image')))
            elif action == 'accept':
                services.buyer_accept(claim)
            elif action == 'escalate':
                services.escalate(claim, 'buyer', request.user)
            elif action == 'withdraw':
                services.withdraw(claim, request.user)
            else:
                return Response({'detail': 'Unknown action.'}, status=404)
        except services.ClaimError as exc:
            return _err(exc)
        claim = _base().prefetch_related('messages').get(pk=claim.pk)
        return Response(_claim_json(claim, request.user, True))


# ── Seller ───────────────────────────────────────────────────────────────────

class SellerClaimsView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_ORDERS)]

    def get(self, request):
        vendor = get_current_vendor(request.user)
        qs = _base().filter(vendor=vendor)
        f = request.query_params.get('filter', 'active')
        if f == 'active':
            qs = qs.filter(status__in=Claim.ACTIVE)
        elif f == 'closed':
            qs = qs.exclude(status__in=Claim.ACTIVE)
        pager = Paging()
        page = pager.paginate_queryset(qs, request, view=self)
        resp = pager.get_paginated_response([_claim_json(c, request.user) for c in page])
        resp.data['needs_response'] = _base().filter(vendor=vendor, status=Claim.OPEN).count()
        return resp


class SellerClaimView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_ORDERS)]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request, ref):
        claim = get_object_or_404(_base().prefetch_related('messages'), reference=ref,
                                  vendor=get_current_vendor(request.user))
        return Response(_claim_json(claim, request.user, True))

    def post(self, request, ref, action=None):
        claim = get_object_or_404(_base(), reference=ref, vendor=get_current_vendor(request.user))
        try:
            if action == 'messages':
                services.add_message(claim, ClaimMessage.SELLER, request.user, request.data.get('text'),
                                     image=_image(request.FILES.get('image')))
            elif action == 'offer':
                services.seller_offer(claim, request.user, request.data.get('amount') or 0, request.data.get('note') or '')
            else:
                return Response({'detail': 'Unknown action.'}, status=404)
        except (services.ClaimError, ArithmeticError, ValueError) as exc:
            status_code = getattr(exc, 'status', 400)
            return Response({'detail': str(exc) if isinstance(exc, services.ClaimError) else 'Enter an amount.'},
                            status=status_code)
        claim = _base().prefetch_related('messages').get(pk=claim.pk)
        return Response(_claim_json(claim, request.user, True))

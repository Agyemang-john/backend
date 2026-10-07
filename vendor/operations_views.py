"""
vendor/operations_views.py
Seller endpoints for running the store day to day.

    GET        action-center/                    what needs attention now (by team role)
    GET        plan/                             plan limits, for greying out paid features
    GET/PATCH  operations-settings/              handling days, low-stock threshold
    GET        finance/balance/                  available / pending / processing / paid
    GET        finance/ledger/                   statement lines (paginated, filterable)
    GET        finance/statement.csv             statement export          [plan: can_export_reports]
    GET        orders/export.csv                 orders export             [plan: can_export_reports]
    GET        returns/                          return requests
    POST       returns/<ref>/<approve|reject|receive>/
    GET        reviews/manage/                   reviews with reply state
    PUT/DELETE reviews/<id>/reply/               public seller reply
    POST       reviews/<id>/report/              flag a review for Negromart staff

Access: every view requires an active membership in an approved store
(vendor/access.py); capabilities decide what each team role may do.
"""

import csv
from datetime import datetime, time

from django.db import IntegrityError, transaction
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from order import returns as return_rules
from order.fulfilment import ship_by_for
from order.models import Order, ReturnRequest
from payments.entitlements import plan_summary
from payments.ledger import balance_summary
from payments.ledger_models import LedgerEntry
from payments.models import Payout
from payments.subscription_permissions import require_feature
from product.models import ProductReview, ReviewReport

from .access import ANY_MEMBER, Capability, IsVendorMember, get_membership, require_capability
from .action_center import build_action_list
from .operations_serializers import (
    LedgerEntrySerializer, OperationsSettingsSerializer, PayoutSummarySerializer,
    ReturnDecisionSerializer, ReviewReplySerializer, ReviewReportSerializer,
    SellerReviewSerializer, VendorReturnSerializer,
)


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


def _vendor(request):
    return get_membership(request.user).vendor


def _date_range(request):
    """?from=YYYY-MM-DD&to=YYYY-MM-DD → aware datetimes (inclusive), either may be None."""
    tz = timezone.get_current_timezone()
    start = parse_date(request.query_params.get('from') or '')
    end = parse_date(request.query_params.get('to') or '')
    return (
        timezone.make_aware(datetime.combine(start, time.min), tz) if start else None,
        timezone.make_aware(datetime.combine(end, time.max), tz) if end else None,
    )


class _Echo:
    """File-like object for streaming CSV rows without building the file in memory."""
    def write(self, value):
        return value


def _csv_response(filename, header, rows):
    writer = csv.writer(_Echo())
    stream = (writer.writerow(row) for row in _chain([header], rows))
    response = StreamingHttpResponse(stream, content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _chain(*iterables):
    for it in iterables:
        yield from it


# ── Overview ──────────────────────────────────────────────────────────────────

class ActionCenterView(APIView):
    permission_classes = [IsAuthenticated, IsVendorMember]

    def get(self, request):
        return Response({'items': build_action_list(get_membership(request.user))})


class PlanSummaryView(APIView):
    permission_classes = [IsAuthenticated, IsVendorMember]

    def get(self, request):
        return Response(plan_summary(_vendor(request)))


class OperationsSettingsView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_STORE, read=ANY_MEMBER)]

    def get(self, request):
        return Response(OperationsSettingsSerializer(_vendor(request)).data)

    def patch(self, request):
        serializer = OperationsSettingsSerializer(_vendor(request), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ── Money ─────────────────────────────────────────────────────────────────────

class BalanceView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.VIEW_FINANCE)]

    def get(self, request):
        vendor = _vendor(request)
        recent = Payout.objects.filter(vendor=vendor).order_by('-created_at')[:5]
        return Response({
            **balance_summary(vendor),
            'plan': plan_summary(vendor),
            'recent_payouts': PayoutSummarySerializer(recent, many=True).data,
        })


def _ledger_queryset(request):
    qs = LedgerEntry.objects.filter(vendor=_vendor(request)).select_related('order')
    start, end = _date_range(request)
    if start:
        qs = qs.filter(created_at__gte=start)
    if end:
        qs = qs.filter(created_at__lte=end)
    entry_type = request.query_params.get('type')
    if entry_type in dict(LedgerEntry.TYPE_CHOICES):
        qs = qs.filter(entry_type=entry_type)
    return qs


class LedgerView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.VIEW_FINANCE)]

    def get(self, request):
        paginator = StandardPagination()
        page = paginator.paginate_queryset(_ledger_queryset(request), request, view=self)
        data = LedgerEntrySerializer(page, many=True, context={'now': timezone.now()}).data
        return paginator.get_paginated_response(data)


class StatementExportView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.VIEW_FINANCE),
                          require_feature('can_export_reports')]

    def get(self, request):
        now = timezone.now()
        entries = _ledger_queryset(request).order_by('created_at', 'id')
        rows = (
            [e.created_at.strftime('%Y-%m-%d %H:%M'), e.get_entry_type_display(), e.description,
             e.order.order_number if e.order else '', f'{e.amount:.2f}', e.currency,
             'paid' if e.payout_id else ('available' if e.available_at <= now else 'pending'),
             e.available_at.strftime('%Y-%m-%d')]
            for e in entries.iterator(chunk_size=1000)
        )
        header = ['Date', 'Type', 'Description', 'Order', 'Amount', 'Currency', 'Status', 'Available from']
        return _csv_response(f'negromart-statement-{now:%Y%m%d}.csv', header, rows)


class OrdersExportView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_ORDERS),
                          require_feature('can_export_reports')]

    def get(self, request):
        vendor = _vendor(request)
        orders = Order.objects.filter(vendors=vendor, is_ordered=True).order_by('-date_created')
        start, end = _date_range(request)
        if start:
            orders = orders.filter(date_created__gte=start)
        if end:
            orders = orders.filter(date_created__lte=end)

        def rows():
            for order in orders.prefetch_related('order_products__product', 'shipments').iterator(chunk_size=500):
                lines = [op for op in order.order_products.all() if op.product and op.product.vendor_id == vendor.pk]
                shipment = next((s for s in order.shipments.all() if s.vendor_id == vendor.pk), None)
                for line in lines:
                    yield [
                        order.order_number, order.date_created.strftime('%Y-%m-%d %H:%M'), order.get_status_display(),
                        line.product.title, line.product.seller_sku or line.product.sku, line.quantity,
                        f'{line.price:.2f}', f'{line.amount:.2f}', order.get_payment_method_display(),
                        ship_by_for(order, vendor).strftime('%Y-%m-%d'),
                        shipment.get_status_display() if shipment else 'Not shipped',
                        (shipment.tracking_number or '') if shipment else '',
                    ]

        header = ['Order', 'Date', 'Order status', 'Product', 'SKU', 'Qty', 'Unit price', 'Line total',
                  'Payment', 'Ship by', 'Shipment', 'Tracking number']
        return _csv_response(f'negromart-orders-{timezone.now():%Y%m%d}.csv', header, rows())


# ── Returns ───────────────────────────────────────────────────────────────────

class VendorReturnListView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_ORDERS)]

    def get(self, request):
        qs = (
            ReturnRequest.objects.filter(vendor=_vendor(request))
            .select_related('order', 'order_product__product', 'customer')
        )
        status_filter = request.query_params.get('status')
        if status_filter in dict(ReturnRequest.STATUS_CHOICES):
            qs = qs.filter(status=status_filter)
        reference = request.query_params.get('ref')
        if reference:
            qs = qs.filter(reference=reference)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            VendorReturnSerializer(page, many=True, context={'request': request}).data
        )


class VendorReturnActionView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_ORDERS)]
    ACTIONS = {
        'approve': lambda rr, user, note: return_rules.approve(rr, user, note),
        'reject': lambda rr, user, note: return_rules.reject(rr, user, note),
        'receive': lambda rr, user, note: return_rules.mark_received(rr, user),
    }

    def post(self, request, reference, action):
        handler = self.ACTIONS.get(action)
        if handler is None:
            return Response({'detail': 'Unknown action.'}, status=status.HTTP_404_NOT_FOUND)
        rr = get_object_or_404(ReturnRequest, reference=reference, vendor=_vendor(request))
        payload = ReturnDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            handler(rr, request.user, payload.validated_data['note'])
        except return_rules.ReturnError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorReturnSerializer(rr, context={'request': request}).data)


# ── Reviews ───────────────────────────────────────────────────────────────────

class SellerReviewListView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG)]

    def get(self, request):
        qs = (
            # Sellers see their products' published reviews only; pending,
            # rejected and hidden ones are Negromart's moderation business.
            ProductReview.objects.filter(vendor=_vendor(request), moderation_status=ProductReview.APPROVED)
            .select_related('product', 'user').prefetch_related('reports', 'media')
        )
        if request.query_params.get('filter') == 'unanswered':
            qs = qs.filter(seller_reply='')
        rating = request.query_params.get('rating')
        if rating and rating.isdigit():
            qs = qs.filter(rating=int(rating))
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            SellerReviewSerializer(page, many=True, context={'request': request}).data
        )


def _own_review(request, pk):
    return get_object_or_404(ProductReview.objects.prefetch_related('reports'), pk=pk, vendor=_vendor(request),
                             moderation_status=ProductReview.APPROVED)


class ReviewReplyView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG)]

    def put(self, request, pk):
        review = _own_review(request, pk)
        payload = ReviewReplySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        review.seller_reply = payload.validated_data['reply']
        review.seller_replied_at = timezone.now()
        review.seller_replied_by = request.user
        review.save(update_fields=['seller_reply', 'seller_replied_at', 'seller_replied_by', 'updated'])
        return Response(SellerReviewSerializer(review).data)

    def delete(self, request, pk):
        review = _own_review(request, pk)
        review.seller_reply = ''
        review.seller_replied_at = None
        review.seller_replied_by = None
        review.save(update_fields=['seller_reply', 'seller_replied_at', 'seller_replied_by', 'updated'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReviewReportView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG)]

    def post(self, request, pk):
        review = _own_review(request, pk)
        payload = ReviewReportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            with transaction.atomic():  # savepoint, so a duplicate doesn't break the request
                ReviewReport.objects.create(review=review, vendor=_vendor(request), reported_by=request.user,
                                            **payload.validated_data)
        except IntegrityError:
            return Response({'detail': 'You have already reported this review. Our team will look at it.'},
                            status=status.HTTP_409_CONFLICT)
        return Response({'detail': 'Thanks. Our team will review it.'}, status=status.HTTP_201_CREATED)

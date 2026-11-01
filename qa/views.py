from django.db.models import Case, F, IntegerField, Q, Value, When, Window
from django.db.models.functions import RowNumber
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from product.models import Product
from vendor.access import Capability, get_current_vendor, require_capability

from . import services
from .models import AnswerVote, ProductAnswer, ProductQuestion

ANSWERS_PREVIEW = 2


class QAPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 30


def _display_name(user):
    if user is None:
        return 'Former customer'
    first = (user.first_name or '').strip() or 'Shopper'
    last = (user.last_name or '').strip()
    return f"{first} {last[0]}." if last else first


def _answer_json(a, voted_ids):
    return {
        'id': a.pk,
        'text': a.text,
        'author': a.vendor.name if a.vendor_id else _display_name(a.user),
        'is_seller': a.vendor_id is not None,
        'is_verified_buyer': a.is_verified_buyer,
        'helpful_count': a.helpful_count,
        'voted': a.pk in voted_ids,
        'is_mine': False,
        'created_at': a.created_at,
    }


def _ranked_answers():
    """Seller answers first, then the most helpful, then oldest."""
    return (ProductAnswer.objects.filter(status=ProductAnswer.PUBLISHED)
            .annotate(seller_first=Case(When(vendor__isnull=False, then=Value(0)), default=Value(1),
                                        output_field=IntegerField()))
            .select_related('user', 'vendor')
            .order_by('seller_first', '-helpful_count', 'created_at'))


def _with_answers(questions, request, preview=True):
    ids = [q.pk for q in questions]
    qs = _ranked_answers().filter(question_id__in=ids)
    if preview:
        qs = qs.annotate(rank=Window(RowNumber(), partition_by=[F('question_id')],
                                     order_by=[F('seller_first').asc(), F('helpful_count').desc(),
                                               F('created_at').asc()])).filter(rank__lte=ANSWERS_PREVIEW)
    answers = list(qs)
    user = request.user if request.user.is_authenticated else None
    voted = set(AnswerVote.objects.filter(user=user, answer__in=[a.pk for a in answers])
                .values_list('answer_id', flat=True)) if user else set()
    grouped = {}
    for a in answers:
        row = _answer_json(a, voted)
        row['is_mine'] = bool(user and a.user_id == user.pk)
        grouped.setdefault(a.question_id, []).append(row)
    return [{
        'id': q.pk,
        'text': q.text,
        'author': _display_name(q.user),
        'answer_count': q.answer_count,
        'seller_answered': q.seller_answered,
        'is_mine': bool(user and q.user_id == user.pk),
        'created_at': q.created_at,
        'answers': grouped.get(q.pk, []),
    } for q in questions]


def _error(exc):
    return Response({'detail': str(exc)}, status=exc.status)


class ProductQuestionsView(APIView):
    """GET a product's questions (?q= search, ?sort=helpful|recent) · POST {text} to ask."""

    def get_permissions(self):
        return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def get(self, request, product_id):
        qs = (ProductQuestion.objects.filter(product_id=product_id, status=ProductQuestion.PUBLISHED)
              .select_related('user'))
        term = (request.query_params.get('q') or '').strip()[:100]
        if term:
            qs = qs.filter(Q(text__icontains=term) | Q(answers__text__icontains=term,
                                                          answers__status=ProductAnswer.PUBLISHED)).distinct()
        if request.query_params.get('sort') == 'recent':
            qs = qs.order_by('-created_at')
        else:
            qs = qs.order_by('-answer_count', '-created_at')
        paginator = QAPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        resp = paginator.get_paginated_response(_with_answers(page, request))
        resp.data['can_answer_as_seller'] = False
        from vendor.access import signed_in_as_seller
        if request.user.is_authenticated and signed_in_as_seller(request.user):
            product = Product.objects.filter(pk=product_id).only('pk', 'vendor_id').first()
            resp.data['can_answer_as_seller'] = bool(product and services.seller_membership_for(request.user, product))
        return resp

    def post(self, request, product_id):
        product = Product.objects.filter(pk=product_id).first()
        try:
            q = services.ask(request.user, product, request.data.get('text'))
        except services.QAError as exc:
            return _error(exc)
        return Response(_with_answers([q], request)[0], status=status.HTTP_201_CREATED)


class QuestionAnswersView(APIView):
    """GET every answer to one question · POST {text} to answer."""

    def get_permissions(self):
        return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def get(self, request, pk):
        q = get_object_or_404(ProductQuestion.objects.select_related('user'), pk=pk, status=ProductQuestion.PUBLISHED)
        return Response(_with_answers([q], request, preview=False)[0])

    def post(self, request, pk):
        q = get_object_or_404(ProductQuestion.objects.select_related('product'), pk=pk)
        try:
            a = services.answer(request.user, q, request.data.get('text'))
        except services.QAError as exc:
            return _error(exc)
        a = _ranked_answers().get(pk=a.pk)
        row = _answer_json(a, set())
        row['is_mine'] = True
        return Response(row, status=status.HTTP_201_CREATED)


class QuestionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        q = get_object_or_404(ProductQuestion, pk=pk, user=request.user)
        q.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AnswerDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        a = get_object_or_404(ProductAnswer, pk=pk)
        try:
            services.delete_answer(request.user, a)
        except services.QAError as exc:
            return _error(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AnswerHelpfulView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        a = get_object_or_404(ProductAnswer, pk=pk, status=ProductAnswer.PUBLISHED)
        try:
            voted, count = services.toggle_helpful(request.user, a)
        except services.QAError as exc:
            return _error(exc)
        return Response({'voted': voted, 'helpful_count': count})


class ReportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, kind, pk):
        try:
            if kind == 'questions':
                services.report(request.user, request.data.get('reason'),
                                question=get_object_or_404(ProductQuestion, pk=pk))
            else:
                services.report(request.user, request.data.get('reason'),
                                answer_obj=get_object_or_404(ProductAnswer, pk=pk))
        except services.QAError as exc:
            return _error(exc)
        return Response({'detail': 'Thanks. Our team will take a look.'}, status=status.HTTP_201_CREATED)


class SellerQuestionsView(APIView):
    """Seller dashboard inbox: ?filter=unanswered|answered|all, ?product=<id>."""
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG)]

    def get(self, request):
        vendor = get_current_vendor(request.user)
        qs = (ProductQuestion.objects.filter(product__vendor=vendor, status=ProductQuestion.PUBLISHED)
              .select_related('user', 'product').order_by('-created_at'))
        f = request.query_params.get('filter', 'unanswered')
        if f == 'unanswered':
            qs = qs.filter(seller_answered=False)
        elif f == 'answered':
            qs = qs.filter(seller_answered=True)
        product_id = request.query_params.get('product')
        if product_id and product_id.isdigit():
            qs = qs.filter(product_id=product_id)
        paginator = QAPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        rows = _with_answers(page, request, preview=False)
        for row, q in zip(rows, page):
            p = q.product
            try:
                image = p.image.url if p.image else ''
            except ValueError:
                image = ''
            row['product'] = {'id': p.pk, 'title': p.title, 'sku': p.sku, 'slug': p.slug, 'image': image}
        resp = paginator.get_paginated_response(rows)
        resp.data['unanswered_total'] = ProductQuestion.objects.filter(
            product__vendor=vendor, status=ProductQuestion.PUBLISHED, seller_answered=False).count()
        return resp

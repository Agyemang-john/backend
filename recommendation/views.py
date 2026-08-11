"""
recommendation/views.py

The storefront API. Each rail is one endpoint, each endpoint is a thin wrapper
around serving.py — retrieval logic lives there so it can be reused by the
existing core/product views without going through HTTP.

Everything here works with no trained model and no logged-in user: serving.py's
fallback chain guarantees a populated rail, so the frontend can be built against
these endpoints on day one and simply gets better as the models train.
"""

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from product.models import Product

from . import serving
from .models import ModelRun, NotInterested, ProductDealScore
from .serializers import (
    DealCardSerializer, RecommendedProductSerializer, TrackEventSerializer, apply_currency,
)
from .tasks import log_recommendation_events

logger = logging.getLogger(__name__)


def _limit(request, default: int, ceiling: int = 60) -> int:
    try:
        return max(1, min(int(request.query_params.get('limit', default)), ceiling))
    except (TypeError, ValueError):
        return default


def _respond(request, products, reasons=None, serializer_class=RecommendedProductSerializer, **context):
    """Serialise a rail and convert prices to the caller's currency."""
    payload = serializer_class(
        products, many=True,
        context={'request': request, 'reasons': reasons or {}, **context},
    ).data
    currency = request.headers.get('X-Currency', 'GHS')
    return Response({
        'count': len(payload),
        'currency': currency,
        'results': apply_currency(payload, currency),
    })


class TodaysDealsAPIView(APIView):
    """
    GET /api/v1/recommendations/todays-deals/

    Deals ranked by learned quality — discount depth verified against the
    product's own price history, weighted by demand, rating, scarcity and
    freshness — then nudged toward the visitor's browsing interests.

    ?debug=1 returns the component scores behind each position.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        limit = _limit(request, 20)
        products = serving.todays_deals(request, limit)

        deal_scores = {
            deal.product_id: deal
            for deal in ProductDealScore.objects.filter(product_id__in=[p.id for p in products])
        }
        return _respond(
            request, products,
            serializer_class=DealCardSerializer,
            deal_scores=deal_scores,
            include_debug=request.query_params.get('debug') == '1',
        )


class RecommendedForYouAPIView(APIView):
    """
    GET /api/v1/recommendations/for-you/

    Signed-in shoppers get their precomputed rail. Guests get one built live from
    the session's recently viewed products — personal after a single view, which
    is when it counts most.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        limit = _limit(request, 20)
        products, reasons = serving.recommended_for_you(request, limit)
        return _respond(request, products, reasons)


class YouMightAlsoLikeAPIView(APIView):
    """
    GET /api/v1/recommendations/similar/<sku>/<slug>/

    Every rail the product page needs, in one response: substitutes from the
    blended collaborative + content neighbour model, complements from
    co-purchase, and the seller's other listings. Three round trips would buy
    nothing — the page renders them together.
    """

    permission_classes = [AllowAny]

    def get(self, request, sku, slug):
        product = (
            Product.published.filter(sku=sku, slug=slug).only('id', 'vendor_id').first()
            or Product.published.filter(sku=sku).only('id', 'vendor_id').first()
        )
        if product is None:
            return Response({'detail': 'Product not found.'}, status=status.HTTP_404_NOT_FOUND)

        limit = _limit(request, 12)
        currency = request.headers.get('X-Currency', 'GHS')
        context = {'request': request, 'reasons': {}}

        def serialize(products):
            return apply_currency(
                RecommendedProductSerializer(products, many=True, context=context).data,
                currency,
            )

        return Response({
            'currency': currency,
            'you_might_also_like': serialize(serving.you_might_also_like(product.id, limit)),
            'customers_also_bought': serialize(serving.customers_also_bought(product.id, limit)),
            'more_from_seller': serialize(
                serving.more_from_seller(product.id, product.vendor_id, limit)
            ),
        })


class CartAddonsAPIView(APIView):
    """
    GET /api/v1/recommendations/cart-addons/

    "Complete your order" — co-purchase scores summed across the whole basket, so
    a product that complements several cart items outranks one that strongly
    complements a single item. Works for guest carts via the X-Guest-Cart header
    the storefront already sends.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        limit = _limit(request, 10)
        cart_product_ids = self._cart_product_ids(request)
        products = serving.cart_addons(cart_product_ids, limit)
        return _respond(request, products)

    def _cart_product_ids(self, request) -> list[int]:
        import json

        if request.user.is_authenticated:
            from order.models import Cart
            cart = Cart.objects.filter(user=request.user).first()
            if cart:
                return list(
                    cart.cart_items.filter(product__isnull=False)
                    .values_list('product_id', flat=True)
                )
            return []

        raw = request.headers.get('X-Guest-Cart')
        if not raw:
            return []
        try:
            items = json.loads(raw)
            return [int(item['p']) for item in items if item.get('p')]
        except (ValueError, TypeError, KeyError):
            return []


class KeepShoppingAPIView(APIView):
    """
    GET /api/v1/recommendations/keep-shopping/

    The visitor's recently viewed products, newest first.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        products = serving.keep_shopping(request, _limit(request, 12))
        return _respond(request, products)


class TrackEventAPIView(APIView):
    """
    POST /api/v1/recommendations/track/

    Impression and click beacons from the rails. Accepts one event or a list, and
    hands them to Celery immediately — the caller never waits on a write.

    This is what makes the rails measurable: CTR per surface today, and the
    labelled training data for a learned ranker later.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        payload = request.data if isinstance(request.data, list) else [request.data]
        serializer = TrackEventSerializer(data=payload, many=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        key = serving.visitor_key(request) or ''
        user_id = request.user.pk if request.user.is_authenticated else None
        model_run_id = serving.current_model_version() or None

        events = [
            {**event, 'user_id': user_id, 'visitor_key': key, 'model_run_id': model_run_id}
            for event in serializer.validated_data
        ]

        try:
            log_recommendation_events.delay(events)
        except Exception:
            # Analytics must never break a page. Losing a beacon is acceptable;
            # a 500 on the storefront is not.
            logger.warning('track: could not enqueue %d recommendation event(s)', len(events))

        return Response({'accepted': len(events)}, status=status.HTTP_202_ACCEPTED)


class NotInterestedAPIView(APIView):
    """
    POST   /api/v1/recommendations/not-interested/   {"product_id": 123}
    DELETE /api/v1/recommendations/not-interested/   {"product_id": 123}

    Explicit negative feedback. Implicit signals can only ever guess at
    disinterest; this records it outright, and it hard-filters the product from
    every rail from the next request onward.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        product_id = request.data.get('product_id')
        if not product_id:
            return Response({'detail': 'product_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not Product.objects.filter(pk=product_id).exists():
            return Response({'detail': 'Product not found.'}, status=status.HTTP_404_NOT_FOUND)

        key = serving.visitor_key(request) or ''
        if not request.user.is_authenticated and not key:
            return Response(
                {'detail': 'Sign in or enable cookies to hide products.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        NotInterested.objects.get_or_create(
            user=request.user if request.user.is_authenticated else None,
            visitor_key='' if request.user.is_authenticated else key,
            product_id=product_id,
        )
        return Response({'status': 'hidden'}, status=status.HTTP_201_CREATED)

    def delete(self, request):
        product_id = request.data.get('product_id')
        queryset = NotInterested.objects.filter(product_id=product_id)
        if request.user.is_authenticated:
            queryset.filter(user=request.user).delete()
        else:
            key = serving.visitor_key(request) or ''
            if key:
                queryset.filter(visitor_key=key).delete()
        return Response({'status': 'restored'})


class ModelHealthAPIView(APIView):
    """
    GET /api/v1/recommendations/health/

    Staff-only. Reports the live model's shape, its offline metrics and — the
    number that matters — how it compares against the most-popular baseline.
    A recommender that cannot beat "show everyone the best-sellers" is costing
    compute for nothing, and this is where that shows up.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_staff:
            return Response({'detail': 'Staff only.'}, status=status.HTTP_403_FORBIDDEN)

        run = (
            ModelRun.objects
            .filter(status=ModelRun.STATUS_COMPLETED)
            .order_by('-finished_at')
            .first()
        )
        if run is None:
            return Response({'trained': False, 'detail': 'No completed training run yet.'})

        return Response({
            'trained': True,
            'run_id': run.pk,
            'finished_at': run.finished_at,
            'duration_seconds': run.duration_seconds,
            'dataset': {
                'shoppers': run.n_users,
                'products': run.n_items,
                'interactions': run.n_interactions,
                'sparsity': run.sparsity,
            },
            'hyperparameters': {
                'factors': run.factors,
                'iterations': run.iterations,
                'regularization': run.regularization,
                'alpha': run.alpha,
            },
            'cf_weight': run.cf_weight,
            'metrics': {
                'precision_at_10': run.precision_at_10,
                'recall_at_10': run.recall_at_10,
                'map_at_10': run.map_at_10,
                'ndcg_at_10': run.ndcg_at_10,
                'catalog_coverage': run.catalog_coverage,
                'baseline_precision_at_10': run.baseline_precision_at_10,
                'lift_over_baseline': run.lift_over_baseline,
            },
            'notes': run.notes,
        })

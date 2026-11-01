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


def _sponsored_deals(request, currency, flash_end):
    """Advertised products on a live deal, as deal cards marked sponsored (ads/)."""
    from ads import services as ad_services

    from .deals_page import _deal_rows
    won = ad_services.sponsored_products('deals', scope=[row['id'] for row in _deal_rows()], scope_key='all')
    if not won:
        return []
    products = [p for p, _ in won]
    deal_scores = {d.product_id: d for d in ProductDealScore.objects.filter(product_id__in=[p.pk for p in products])}
    cards = DealCardSerializer(products, many=True, context={
        'request': request, 'reasons': {}, 'deal_scores': deal_scores,
        'flash_end': flash_end, 'include_debug': False,
    }).data
    for card, (_, token) in zip(cards, won):
        card['sponsored'], card['ad_token'] = True, token
    return apply_currency(cards, currency)


class DealsPageAPIView(APIView):
    """
    GET /api/v1/recommendations/deals/

    The full Today's Deals page: every live deal, with department and category
    chips (with counts), discount-depth and flash-deal filters, the usual
    price / brand / seller / rating filters, and seven sorts. The default sort
    is the deal model's ranking, personalised and capped per seller.

    Query: department, category, brand*, vendor*, rating*, discount, flash=1,
    from, to (in the caller's currency), sort, page.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        from decimal import Decimal

        from core.service import get_exchange_rates
        from product.serializers import CollectionSerializer

        from .deals_page import deals_page

        currency = request.headers.get('X-Currency', 'GHS')
        rate = Decimal(str(get_exchange_rates().get(currency, 1)))
        data = deals_page(request, rate)

        products = serving._ordered_by_ids(data['page_ids'])
        deal_scores = {
            deal.product_id: deal
            for deal in ProductDealScore.objects.filter(product_id__in=data['page_ids'])
        }
        cards = DealCardSerializer(products, many=True, context={
            'request': request, 'reasons': {}, 'deal_scores': deal_scores,
            'flash_end': data['flash_end'], 'include_debug': False,
        }).data

        to_currency = float(rate)
        collection = data['collection']
        return Response({
            'collection': CollectionSerializer(collection, context={'request': request}).data
                          if collection else None,
            'currency': currency,
            'results': apply_currency(cards, currency),
            # Sponsored deals (ads/): advertised products that are on a live deal, first page only.
            'sponsored': _sponsored_deals(request, currency, data['flash_end']) if data['page'] == 1 else [],
            'total': data['total'],
            'page': data['page'],
            'pages': data['pages'],
            'page_size': data['page_size'],
            'sort': data['sort'],
            'sorts': data['sorts'],
            'departments': data['departments'],
            'all_count': data['all_count'],
            'categories': data['categories'],
            'discount_buckets': data['discount_buckets'],
            'flash_count': data['flash_count'],
            'brands': data['brands'],
            'vendors': data['vendors'],
            'sizes': [],
            'colors': [],
            'min_price_unfiltered': round(data['min_price'] * to_currency, 2),
            'max_price_unfiltered': round(data['max_price'] * to_currency, 2),
        })


class CategoryShelvesAPIView(APIView):
    """
    GET /api/v1/recommendations/category-shelves/?shelves=4&limit=12

    "Best sellers in Phones", "Trending in Fashion" — one shelf for each of the
    shopper's top categories (from views, cart and orders), topped up with the
    marketplace's busiest categories for a first-time visitor.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        from .shelves import category_shelves

        try:
            count = max(1, min(int(request.query_params.get('shelves', 4)), 8))
        except (TypeError, ValueError):
            count = 4
        per_shelf = _limit(request, 12, ceiling=24)
        currency = request.headers.get('X-Currency', 'GHS')

        shelves = category_shelves(request, shelves=count, per_shelf=per_shelf)
        result = []
        for shelf in shelves:
            products = serving._ordered_by_ids(shelf['product_ids'])
            cards = RecommendedProductSerializer(
                products, many=True, context={'request': request, 'reasons': {}},
            ).data
            result.append({
                'category': shelf['category'],
                'kind': shelf['kind'],
                'title': shelf['title'],
                'results': apply_currency(cards, currency),
            })
        return Response({'currency': currency, 'shelves': result})


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
        cart_product_ids = serving._cart_product_ids(request)
        products = serving.cart_addons(cart_product_ids, limit)
        return _respond(request, products)


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


class FrequentlyBoughtTogetherAPIView(APIView):
    """
    GET /api/v1/recommendations/frequently-bought/<product_id>/

    This product plus up to two complements and their combined price, for the
    "Frequently bought together" box (serving.frequently_bought_together).
    """

    permission_classes = [AllowAny]

    def get(self, request, pk):
        from decimal import Decimal

        from .serializers import RecommendedProductSerializer, apply_currency

        product = Product.published.filter(pk=pk).first()
        if product is None:
            return Response({'detail': 'Product not found.'}, status=status.HTTP_404_NOT_FOUND)
        currency = request.headers.get('X-Currency', 'GHS')
        picks = serving.frequently_bought_together(product.id)
        if not picks:
            return Response({'currency': currency, 'items': [], 'total': None})
        cards = apply_currency(
            RecommendedProductSerializer([product] + picks, many=True,
                                         context={'request': request, 'reasons': {}}).data,
            currency,
        )
        # Items sold in sizes/colours can't be added blind: the box links to them instead.
        kinds = dict(Product.objects.filter(pk__in=[c['id'] for c in cards]).values_list('pk', 'variant'))
        for c in cards:
            c['needs_options'] = kinds.get(c['id'], 'None') != 'None'
        total = sum((Decimal(str(c['price'])) for c in cards if c.get('price') is not None), Decimal('0'))
        return Response({'currency': currency, 'items': cards, 'total': round(total, 2)})


class SellerPinnedComplementsAPIView(APIView):
    """GET/PUT {"others": [product ids]} — a seller's own "buy it with" picks for one of their products (max 3)."""

    MAX = 3

    def get_permissions(self):
        from vendor.access import Capability, require_capability
        return [IsAuthenticated(), require_capability(Capability.MANAGE_CATALOG)()]

    def _product(self, request, pk):
        from django.shortcuts import get_object_or_404

        from vendor.access import get_current_vendor
        return get_object_or_404(Product, pk=pk, vendor=get_current_vendor(request.user))

    def get(self, request, pk):
        from .models import PinnedComplement
        product = self._product(request, pk)
        rows = PinnedComplement.objects.filter(product=product).select_related('other')
        return Response({'others': [{'id': r.other_id, 'title': r.other.title} for r in rows]})

    def put(self, request, pk):
        from django.core.cache import cache
        from django.db import transaction

        from .models import PinnedComplement
        product = self._product(request, pk)
        raw = request.data.get('others')
        if not isinstance(raw, list) or len(raw) > self.MAX or not all(isinstance(i, int) for i in raw):
            return Response({'detail': f'Send up to {self.MAX} product ids.'}, status=status.HTTP_400_BAD_REQUEST)
        ids = [i for i in dict.fromkeys(raw) if i != product.pk]
        own = set(Product.objects.filter(pk__in=ids, vendor_id=product.vendor_id).values_list('pk', flat=True))
        if set(ids) - own:
            return Response({'detail': 'Pick products from your own store.'}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            PinnedComplement.objects.filter(product=product).delete()
            PinnedComplement.objects.bulk_create([PinnedComplement(product=product, other_id=i, position=n)
                                                  for n, i in enumerate(ids)])
        cache.delete(f"rec:fbt:{serving.current_model_version()}:{product.pk}:{serving.FBT_LIMIT}")
        return self.get(request, pk)


class BuyAgainAPIView(APIView):
    """GET /api/v1/recommendations/buy-again/ — things this shopper bought before and can reorder."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .serializers import RecommendedProductSerializer, apply_currency

        currency = request.headers.get('X-Currency', 'GHS')
        rows = serving.buy_again(request.user.pk, _limit(request, 60))
        cards = apply_currency(
            RecommendedProductSerializer([p for p, _, _ in rows], many=True,
                                         context={'request': request, 'reasons': {}}).data,
            currency,
        )
        variants = dict(Product.objects.filter(pk__in=[c['id'] for c in cards]).values_list('pk', 'variant'))
        for card, (_, times, last) in zip(cards, rows):
            card['times_bought'] = times
            card['last_bought'] = last
            card['needs_options'] = variants.get(card['id'], 'None') != 'None'
        return Response({'currency': currency, 'results': cards})

"""
core/views.py
API views for the homepage and supporting data:
- HomeSliderView: promotional sliders with currency conversion
- BannersView: site banners
- MainCategoryWithCategoriesAPIView: navigation menu data
- CategoryDetailView: single category with subcategories
- TopEngagedCategoryView: highest-engagement category
- MainAPIView: combined homepage payload (products, brands, subcategories)
- SearchedProducts: persists search history in cookies

Recommendation rails (deals, recommended-for-you, similar items, cart add-ons)
moved to the `recommendation` app, which serves them from trained models.
"""

from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from .models import *
from order.models import *
from .serializers import *
from product.serializers import ProductSerializer
from django.db.models import Avg, Count, Q
import random
import json
from django.core.cache import cache
from rest_framework import status
from rest_framework.views import APIView
from address.serializers import *
from order.service import *
from rest_framework.permissions import IsAuthenticated, AllowAny
from .service import *
from decimal import Decimal
from product.serializers import LightProductSerializer


from rest_framework.permissions import IsAdminUser

SLIDERS_CACHE_KEY = 'home_sliders_v2'
BANNERS_CACHE_KEY = 'banners_v2'


class HomeSliderView(APIView):
    """Hero slides showing now for this shopper (core/scheduling.py), prices in their currency."""
    permission_classes = [AllowAny]   # public

    def get(self, request):
        from core.scheduling import live_seasons, pick, schedule_of
        from product.regions import storefront_country

        entries = cache.get(SLIDERS_CACHE_KEY)
        if entries is None:
            sliders = HomeSlider.objects.filter(is_active=True).order_by('order', 'id')
            entries = [(HomeSliderSerializer(s, context={'static': True, 'request': request}).data, schedule_of(s))
                       for s in sliders]
            cache.set(SLIDERS_CACHE_KEY, entries, 60 * 60)  # 1 hour; cleared on every save

        shown = pick(entries, live_seasons(storefront_country(request)))

        # Always fresh currency & rates
        currency = request.headers.get('X-Currency', 'GHS')
        exchange_rate = Decimal(str(get_exchange_rates().get(currency, 1)))
        for item in shown:
            if item.get('price') is not None:
                item['price'] = round(Decimal(item['price']) * exchange_rate, 2)
            item['currency'] = currency

        return Response(shown, status=status.HTTP_200_OK)


class PromoGridView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        cache_key = 'promo_grid'
        data = cache.get(cache_key)
        if data is None:
            cards = PromoCard.objects.filter(is_active=True)
            data = PromoCardSerializer(cards, many=True, context={'request': request}).data
            cache.set(cache_key, data, timeout=60 * 30)  # 30 min cache
        return Response(data, status=status.HTTP_200_OK)


class BannersView(APIView):
    """Side banners showing now for this shopper (core/scheduling.py)."""
    permission_classes = [AllowAny]   # public

    def get(self, request):
        from core.scheduling import live_seasons, pick, schedule_of
        from product.regions import storefront_country

        entries = cache.get(BANNERS_CACHE_KEY)
        if entries is None:
            banners = Banners.objects.filter(is_active=True).order_by('order', 'id')
            entries = [(BannersSerializer(b, context={'request': request}).data, schedule_of(b)) for b in banners]
            cache.set(BANNERS_CACHE_KEY, entries, timeout=60 * 60)  # cleared on every save

        return Response(pick(entries, live_seasons(storefront_country(request))), status=status.HTTP_200_OK)

class MainCategoryWithCategoriesAPIView(APIView):
    permission_classes = [AllowAny]   # public
    def get(self, request):
        cache_key = 'main_categories_with_categories'
        cached_data = cache.get(cache_key)
        
        if cached_data is None:
            main_categories = Main_Category.objects.all().order_by('title')
            serializer = MainCategoryWithCategoriesAndSubSerializer(main_categories, many=True, context={'request': request})
            cached_data = serializer.data
            cache.set(cache_key, cached_data, timeout=60 * 60)  # Cache for 15 minutes
        
        return Response(cached_data)

class CategoryDetailView(APIView):
    permission_classes = [AllowAny]   # public
    def get(self, request, slug):
        cache_key = f'category_detail_{slug}'
        cached_data = cache.get(cache_key)

        if cached_data is None:
            category = get_object_or_404(Category, slug=slug)
            serializer = CategoryWithSubcategoriesSerializer(category, context={'request': request})
            cached_data = serializer.data
            cache.set(cache_key, cached_data, timeout=60 * 60)
        else:
            category = Category.objects.filter(slug=slug).first()

        # Track visit — deduped per visitor per 24 h, non-blocking.
        # Buffers into Redis; flush_category_view_counts() drains to DB every 3 min.
        if category:
            from product.utils import track_category_view
            track_category_view(request, category.id)

        return Response(cached_data, status=status.HTTP_200_OK)


class TopEngagedCategoryView(APIView):
    permission_classes = [AllowAny]   # public
    def get(self, request):
        cache_key = 'top_engaged_category'
        cached_data = cache.get(cache_key)
        
        if cached_data is None:
            category = Category.objects.order_by('-engagement_score').first()
            if category:
                serializer = TopEngagedCategorySerializer(category)
                cached_data = serializer.data
            else:
                cached_data = {"detail": "No categories available"}
            cache.set(cache_key, cached_data, timeout=60 * 30)  # Cache for 15 minutes
        
        return Response(cached_data, status=status.HTTP_200_OK if cached_data.get('detail') is None else status.HTTP_404_NOT_FOUND)


def _apply_currency(products_data: list, currency: str, rates: dict) -> list:
    """
    Shared helper — converts price/old_price fields in a list of product dicts
    and stamps the currency. Always works on a fresh copy so the cached list
    is never mutated.
    """
    exchange_rate = Decimal(str(rates.get(currency, 1)))
    converted = []
    for product in products_data:
        p = dict(product)
        p["currency"] = currency
        p["price"] = round(Decimal(str(p["price"])) * exchange_rate, 2)
        if p.get("old_price"):
            p["old_price"] = round(Decimal(str(p["old_price"])) * exchange_rate, 2)
        converted.append(p)
    return converted


class MainAPIView(APIView):
    """Combined homepage payload: new products, most popular, brands, subcategories, top category."""
    permission_classes = [AllowAny]   # public

    def get(self, request, *args, **kwargs):
        cache_key = "homepage_main_v1"
        cached_data = cache.get(cache_key)

        if not cached_data:
            new_products_qs = (
                Product.published.filter(product_type="new")
                .order_by('-date')[:9]
            )
            most_popular_qs = (
                Product.published.all()
                .order_by('-trending_score', '-views')[:8]
            )
            top_brands = Brand.objects.order_by('-engagement_score')[:4]
            subcategories = Sub_Category.objects.order_by('-engagement_score')[:4]
            popular_categories = Category.objects.order_by('-engagement_score')[:4]

            cached_data = {
                "new_products": list(HomepageProductSerializer(new_products_qs, many=True, context={'request': request}).data),
                "most_popular": list(HomepageProductSerializer(most_popular_qs, many=True, context={'request': request}).data),
                "brands": BrandSerializer(top_brands, many=True, context={'request': request}).data,
                "subcategories": SubCategorySerializer(subcategories, many=True, context={'request': request}).data,
                "popular_categories": PopularCategorySerializer(popular_categories, many=True, context={'request': request}).data,
            }
            cache.set(cache_key, cached_data, timeout=600)

        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()

        # Deep-copy and convert — never mutate the cached dict
        response_data = {
            "new_products": _apply_currency(cached_data["new_products"], currency, rates),
            "most_popular": _apply_currency(cached_data["most_popular"], currency, rates),
            "brands": cached_data["brands"],
            "subcategories": cached_data["subcategories"],
            "popular_categories": cached_data["popular_categories"],
        }

        return Response(response_data, status=status.HTTP_200_OK)


class SearchedProducts(APIView):
    permission_classes = [AllowAny]   # public
    def post(self, request):
        from django.utils import timezone as tz

        try:
            search_history = json.loads(request.COOKIES.get('search_history', '[]'))
            if not isinstance(search_history, list):
                search_history = []
        except Exception:
            search_history = []

        new_queries = request.data.get('search_history', [])
        if not isinstance(new_queries, list):
            new_queries = []

        for raw in new_queries:
            if not isinstance(raw, str):
                continue
            query = raw.strip()[:200]
            if not query:
                continue
            if query in search_history:
                search_history.remove(query)
            search_history.insert(0, query)

        search_history = search_history[:10]

        if request.user.is_authenticated:
            for raw in new_queries:
                if not isinstance(raw, str):
                    continue
                query = raw.strip()[:200]
                if not query:
                    continue
                SearchHistory.objects.update_or_create(
                    user=request.user,
                    query=query,
                    defaults={'searched_at': tz.now()},
                )
            # Cap at 50 entries per user — delete oldest beyond that
            old_ids = list(
                SearchHistory.objects.filter(user=request.user)
                .order_by('-searched_at')
                .values_list('id', flat=True)[50:]
            )
            if old_ids:
                SearchHistory.objects.filter(id__in=old_ids).delete()

        response = Response({'status': 'success'}, status=status.HTTP_200_OK)
        response.set_cookie('search_history', json.dumps(search_history), max_age=365*24*60*60, httponly=False)
        return response


class SearchHistoryView(APIView):
    """
    GET  — return the user's search history (DB for auth, cookie for guest).
    DELETE — remove one query ({query: str}) or clear all (no body).
    """
    permission_classes = [AllowAny]   # public

    def get(self, request):
        if request.user.is_authenticated:
            queries = list(
                SearchHistory.objects.filter(user=request.user)
                .order_by('-searched_at')
                .values_list('query', flat=True)[:20]
            )
        else:
            try:
                raw = json.loads(request.COOKIES.get('search_history', '[]'))
                queries = [q for q in raw if isinstance(q, str)][:20]
            except Exception:
                queries = []
        return Response({'queries': queries})

    def delete(self, request):
        if request.user.is_authenticated:
            query = request.data.get('query')
            if query:
                SearchHistory.objects.filter(user=request.user, query=str(query)[:200]).delete()
            else:
                SearchHistory.objects.filter(user=request.user).delete()
        return Response({'status': 'ok'})


#############################CUSTOMER DASHBOARD############################


class SocialLinksView(APIView):
    """Public endpoint — returns all active social/community links for the Community page."""
    permission_classes = [AllowAny]

    def get(self, request):
        links = SocialLink.objects.filter(is_active=True)
        serializer = SocialLinkSerializer(links, many=True, context={'request': request})
        return Response(serializer.data)

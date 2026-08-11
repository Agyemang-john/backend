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
- MakeDefaultAddressView: set/get default address for a user

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
from product.shipping import get_ip_address_from_request, get_user_country_region
from product.serializers import LightProductSerializer


class DebugIPView(APIView):
    """
    Temporary debug endpoint to verify IP detection in production.
    Hit GET /api/debug/ip/ and check what Django sees.
    REMOVE THIS VIEW once IP geolocation is confirmed working.
    """
    permission_classes = []

    def get(self, request):
        ip = get_ip_address_from_request(request)
        country, region = get_user_country_region(request)
        return Response({
            "resolved_ip": ip,
            "country": str(country),
            "region": region,
            "headers": {
                "REMOTE_ADDR": request.META.get("REMOTE_ADDR"),
                "HTTP_X_FORWARDED_FOR": request.META.get("HTTP_X_FORWARDED_FOR"),
                "HTTP_X_REAL_IP": request.META.get("HTTP_X_REAL_IP"),
                "HTTP_CF_CONNECTING_IP": request.META.get("HTTP_CF_CONNECTING_IP"),
                "HTTP_X_CLIENT_IP": request.META.get("HTTP_X_CLIENT_IP"),
            },
        })

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


class HomeSliderView(APIView):

    def get(self, request):
        cache_key = 'home_sliders_static_v1'

        static_data = cache.get(cache_key)
        if static_data is None:
            sliders = (
                HomeSlider.objects
                .filter(is_active=True)
                .only(
                    'id', 'title', 'deal_type', 'price',
                    'price_prefix', 'link_url',
                    'image_mobile', 'image_desktop',
                    'order'
                )
                .order_by('order')
            )

            # Serialize WITHOUT request/currency
            static_data = HomeSliderSerializer(
                sliders,
                many=True,
                context={'static': True, 'request': request}
            ).data

            cache.set(cache_key, static_data, 60 * 60)  # 1 hour

        # Always fresh currency & rates
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()  # fresh or redis-cached

        # Inject dynamic data
        for item in static_data:
            base_price = item.get('price')
            if base_price is not None:
                exchange_rate = Decimal(str(rates.get(currency, 1)))
                item['price'] = round(Decimal(base_price) * exchange_rate, 2)
            item['currency'] = currency

        return Response(static_data, status=status.HTTP_200_OK)

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
    """
    API View to retrieve all banners with caching
    """
    def get(self, request):
        cache_key = 'banners'
        cached_data = cache.get(cache_key)
        
        if cached_data is None:
            banners = Banners.objects.all()
            serializer = BannersSerializer(banners, many=True, context={'request': request})
            cached_data = serializer.data
            cache.set(cache_key, cached_data, timeout=60 * 60)  # Cache for 60 minutes
        
        return Response(cached_data, status=status.HTTP_200_OK)

class MainCategoryWithCategoriesAPIView(APIView):
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
    

class MainAPIView(APIView):
    """Combined homepage payload: new products, most popular, brands, subcategories, top category."""

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


class MakeDefaultAddressView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        # Get the address ID from the request data
        address_id = request.data.get('id')

        if not address_id:
            return Response({"error": "Address ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Set all addresses for the current user to not be default
            Address.objects.filter(user=request.user).update(status=False)

            # Set the selected address as the default
            Address.objects.filter(id=address_id, user=request.user).update(status=True)

            new = Address.objects.filter(status=True, user=request.user).first()

            profile = Profile.objects.select_related('user').get(user=request.user)
            profile.address = new.address
            profile.country = new.country
            profile.mobile = new.mobile
            profile.latitude = new.latitude
            profile.longitude = new.longitude
            profile.save()

            return Response({"success": True, "message": "Address set as default"}, status=status.HTTP_200_OK)

        except Address.DoesNotExist:
            return Response({"error": "Address not found"}, status=status.HTTP_404_NOT_FOUND)
    
    def get(self, request):
        try:
            # Fetch the default address for the authenticated user
            default_address = Address.objects.filter(user=request.user, status=True).first()

            if default_address:
                # Use the serializer to return the default address
                serializer = AddressSerializer(default_address)
                return Response(serializer.data, status=status.HTTP_200_OK)
            else:
                return Response({"message": "No default address found"}, status=status.HTTP_404_NOT_FOUND)

        except Address.DoesNotExist:
            return Response({"error": "Error retrieving default address"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
#############################CUSTOMER DASHBOARD############################


class SocialLinksView(APIView):
    """Public endpoint — returns all active social/community links for the Community page."""
    permission_classes = [AllowAny]

    def get(self, request):
        links = SocialLink.objects.filter(is_active=True)
        serializer = SocialLinkSerializer(links, many=True, context={'request': request})
        return Response(serializer.data)

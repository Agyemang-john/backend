"""
core/views.py
API views for the homepage and supporting data:
- HomeSliderView: promotional sliders with currency conversion
- BannersView: site banners
- MainCategoryWithCategoriesAPIView: navigation menu data
- CategoryDetailView: single category with subcategories
- TopEngagedCategoryView: highest-engagement category
- MainAPIView: combined homepage payload (products, brands, subcategories)
- RecentlyViewedRelatedProductsAPIView: related products based on recently viewed
- SearchedProducts: persists search history in cookies
- RecommendedProducts: personalised recommendations from viewed + searched
- TrendingProductsAPIView: top trending products by score
- SuggestedCartProductsAPIView: suggestions based on current cart contents
- MakeDefaultAddressView: set/get default address for a user
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
            cache.set(cache_key, cached_data, timeout=60 * 60)  # Cache for 15 minutes
        
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
                Product.objects.filter(status='published', product_type="new")
                .annotate(
                    average_rating=Avg('reviews__rating'),
                    review_count=Count('reviews')
                )
                .order_by('-date')[:9]
            )
            most_popular_qs = (
                Product.objects.filter(status='published')
                .annotate(
                    average_rating=Avg('reviews__rating'),
                    review_count=Count('reviews')
                )
                .order_by('-views')[:8]
            )
            category = Category.objects.order_by('-engagement_score').first()
            top_brands = Brand.objects.order_by('-engagement_score')[:4]
            subcategories = Sub_Category.objects.order_by('-engagement_score')[:4]

            cached_data = {
                # Wrap each new product so average_rating/review_count travel with it
                "new_products": list(HomepageProductSerializer(new_products_qs, many=True, context={'request': request}).data),
                "most_popular": list(HomepageProductSerializer(most_popular_qs, many=True, context={'request': request}).data),
                "brands": BrandSerializer(top_brands, many=True, context={'request': request}).data,
                "subcategories": SubCategorySerializer(subcategories, many=True, context={'request': request}).data,
                "category": TopEngagedCategorySerializer(category, context={'request': request}).data if category else None,
            }
            cache.set(cache_key, cached_data, timeout=600)

        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()

        # Deep-copy and convert — never mutate the cached dict
        response_data = {
            "new_products": _apply_currency(cached_data["new_products"], currency, rates),
            "most_popular": _apply_currency(cached_data["most_popular"], currency, rates),
            "brands": cached_data["brands"],           # no prices
            "subcategories": cached_data["subcategories"],  # no prices
            "category": cached_data["category"],            # no prices
        }

        return Response(response_data, status=status.HTTP_200_OK)



class RecentlyViewedRelatedProductsAPIView(APIView):
    def get(self, request):
        product_ids = get_recently_viewed_ids(request)
        if not product_ids:
            return Response([], status=status.HTTP_200_OK)

        position = int(request.query_params.get("position", 0))
        if position >= len(product_ids):
            return Response([], status=status.HTTP_200_OK)

        product_id = product_ids[position]

        try:
            product = Product.published.select_related("sub_category").get(pk=product_id)
        except Product.DoesNotExist:
            return Response([], status=status.HTTP_200_OK)

        if not product.sub_category:
            return Response([], status=status.HTTP_200_OK)

        related_products = (
            Product.published
            .filter(sub_category=product.sub_category)
            .exclude(id=product.id)
            .only('id', 'title', 'slug', 'sku', 'image', 'price', 'old_price')  # light fetch
            [:10]
        )

        serializer = LightProductSerializer(related_products, many=True, context={'request': request})
        return Response(serializer.data)


class SearchedProducts(APIView):
    def post(self, request):
        # Retrieve existing search history from cookies
        search_history = request.COOKIES.get('search_history', '[]')
        search_history = json.loads(search_history)

        # Get the new search queries from the request
        new_searched_queries = request.data.get('search_history', [])

        # Process each query in new_searched_queries
        for query in new_searched_queries:
            # If query already exists, remove it to prevent duplicates
            if query in search_history:
                search_history.remove(query)
            # Insert query at the beginning of the list (most recent first)
            search_history.insert(0, query)

        # Limit search history to the last 10 queries
        if len(search_history) > 10:
            search_history = search_history[:10]

        # Set the updated search history back in cookies
        response = Response({'status': 'success'}, status=status.HTTP_200_OK)
        response.set_cookie('search_history', json.dumps(search_history), max_age=365*24*60*60, httponly=False)  # 1 year
        return response


class RecommendedProducts(APIView):
    def get(self, request):
        # -----------------------------
        # 1. Recently viewed — from Redis
        # -----------------------------
        viewed_product_ids = get_recently_viewed_ids(request)

        viewed_products_qs = (
            Product.published
            .filter(id__in=viewed_product_ids)
            .only('id', 'title', 'slug', 'sku', 'image', 'price', 'old_price', 'sub_category_id')
        )
        # Restore Redis order
        products_dict = {p.id: p for p in viewed_products_qs}
        sorted_viewed_products = [
            products_dict[pid] for pid in viewed_product_ids if pid in products_dict
        ]

        # -----------------------------
        # 2. Related by category — one query instead of N
        # -----------------------------
        sub_category_ids = {
            p.sub_category_id for p in viewed_products_qs if p.sub_category_id
        }
        related_products = (
            Product.published
            .filter(sub_category_id__in=sub_category_ids)
            .exclude(id__in=viewed_product_ids)
            .only('id', 'title', 'slug', 'sku', 'image', 'price', 'old_price')
            .order_by('?')[:20]   # random sample at DB level — no Python shuffle needed
        )

        # -----------------------------
        # 3. Related by search history
        # -----------------------------
        try:
            search_history = json.loads(request.headers.get('X-Search-History', '[]'))
            if not isinstance(search_history, list):
                search_history = []
        except Exception:
            search_history = []

        search_related_products = Product.objects.none()
        if search_history:
            search_q = Q()
            for query in search_history[:5]:   # cap to avoid giant OR chains
                search_q |= Q(title__icontains=query) | Q(description__icontains=query)

            search_related_products = (
                Product.published
                .filter(search_q)
                .exclude(id__in=viewed_product_ids)
                .only('id', 'title', 'slug', 'sku', 'image', 'price', 'old_price')
                .distinct()[:20]
            )

        # -----------------------------
        # 4. Combine and limit
        # -----------------------------
        seen = set()
        combined = []
        for p in list(related_products) + list(search_related_products):
            if p.id not in seen:
                seen.add(p.id)
                combined.append(p)
        recommending_products = combined[:10]

        # Fallback: no history at all → top by views
        if not sorted_viewed_products and not search_history:
            recommending_products = (
                Product.published
                .only('id', 'title', 'slug', 'sku', 'image', 'price', 'old_price')
                .order_by('-views')[:10]
            )

        # -----------------------------
        # 5. Serialize & return
        # -----------------------------
        return Response({
            'recently_viewed': LightProductSerializer(
                sorted_viewed_products, many=True, context={'request': request}
            ).data,
            'recommended_products': LightProductSerializer(
                recommending_products, many=True, context={'request': request}
            ).data,
        })
    

class TrendingProductsAPIView(APIView):
    """Returns top 20 products by trending_score, cached for 10 minutes."""

    def get(self, request):
        products_data = cache.get("top_trending_product")

        if not products_data:
            products = (
                Product.objects.filter(status='published')
                .order_by('-trending_score')[:20]
            )
            # No request context — stores raw GHS prices
            products_data = list(TrendingProductSerializer(products, many=True, context={'request': request}).data)
            cache.set("top_trending_products", products_data, timeout=600)

        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        return Response(_apply_currency(products_data, currency, rates))

# Suggested products based on cart
class SuggestedCartProductsAPIView(APIView):
    def get(self, request):
        try:
            cart_product_ids = []

            if request.user.is_authenticated:
                cart = Cart.objects.get_for_request(request)
                cart_items = cart.cart_items.select_related("product").all() if cart else []
                cart_product_ids = [item.product.id for item in cart_items if item.product]
            else:
                guest_cart_header = request.headers.get('X-Guest-Cart')
                try:
                    guest_cart = json.loads(guest_cart_header) if guest_cart_header else []
                    cart_product_ids = [int(item.get("p")) for item in guest_cart if item.get("p")]
                except Exception as e:
                    return Response({"detail": "Invalid guest cart"}, status=status.HTTP_400_BAD_REQUEST)

            if not cart_product_ids:
                return Response({"suggested": []})

            # Get related subcategories or brands
            products_in_cart = Product.objects.filter(id__in=cart_product_ids)
            sub_categories = products_in_cart.values_list("sub_category", flat=True)
            brands = products_in_cart.values_list("brand", flat=True)

            # Suggest products from same subcategories or brands but not already in cart
            suggested_products = Product.published.filter(
                Q(sub_category__in=sub_categories) | Q(brand__in=brands),
                ~Q(id__in=cart_product_ids),
                status="published"
            ).distinct()[:12]  # limit suggestions

            serialized = ProductSerializer(suggested_products, many=True, context={'request': request}).data
            return Response(serialized)

        except Exception as e:
            return Response(
                {"detail": "Failed to load suggestions", "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class DealsAPIView(APIView):
    """Returns up to 20 products with active discounts, cached for 10 minutes."""

    def get(self, request):
        from django.db.models import F, ExpressionWrapper, FloatField

        products_data = cache.get("deals_products")

        if not products_data:
            products = (
                Product.objects.filter(status="published", old_price__isnull=False)
                .filter(old_price__gt=F("price"))
                .annotate(
                    discount_pct=ExpressionWrapper(
                        (F("old_price") - F("price")) * 100.0 / F("old_price"),
                        output_field=FloatField(),
                    )
                )
                .order_by("-discount_pct")[:20]
            )
            # No request context — stores raw GHS prices
            products_data = list(DealsProductSerializer(products, many=True, context={'request': request}).data)
            cache.set("deals_products", products_data, timeout=600)

        currency = request.headers.get("X-Currency", "GHS")
        rates = get_exchange_rates()
        return Response(_apply_currency(products_data, currency, rates))


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



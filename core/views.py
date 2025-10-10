from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import *
from order.models import *
from .serializers import *
from product.serializers import ProductSerializer, VariantSerializer
from django.db.models import Avg, Count
import random
import json
from django.core.cache import cache
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from address.serializers import *
from order.service import *
from rest_framework.permissions import IsAuthenticated
from .service import *
from django.core.serializers import serialize


class HomeSliderView(APIView):
    """
    API View to retrieve all sliders with caching for static fields
    """
    def get(self, request):
        # Include currency in cache key to support different currencies
        currency = request.headers.get('X-Currency', 'GHS')
        cache_key = f'home_sliders_{currency}'
        cached_data = cache.get(cache_key)
        
        if cached_data is None:
            sliders = HomeSlider.objects.all()
            # Cache the queryset as serialized JSON to avoid pickling issues
            cached_data = json.loads(serialize('json', sliders))
            cache.set(cache_key, cached_data, timeout=60 * 30)  # Cache for 30 minutes
        
        # Convert cached JSON back to queryset-like structure for serialization
        sliders = HomeSlider.objects.filter(pk__in=[item['pk'] for item in cached_data])
        serializer = HomeSliderSerializer(sliders, many=True, context={'request': request})
        
        return Response(serializer.data, status=status.HTTP_200_OK)

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
            cache.set(cache_key, cached_data, timeout=60 * 30)  # Cache for 15 minutes
        
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
    """
    API View to retrieve products, sliders, banners, and subcategories data
    """

    def get(self, request, *args, **kwargs):
        # Get latest products with average ratings
        new_products = Product.objects.filter(status='published', product_type="new").annotate(average_rating=Avg('reviews__rating'), review_count=Count('reviews')).order_by('-date')[:9]
        most_popular = Product.objects.filter(status='published').annotate(average_rating=Avg('reviews__rating'), review_count=Count('reviews')).order_by('-views')[:8]

        category = Category.objects.order_by('-engagement_score').first()

        serializer = TopEngagedCategorySerializer(category, context={'request': request})

        top_brands = Brand.objects.order_by('-engagement_score')[:4]
        
        
        # Serialize new products with ratings
        products_with_details = []
        for product in new_products: 
            # Serialize product data
            product_data = {
                'product': ProductSerializer(product, context={'request': request}).data,  # Serialize the product instance
                'average_rating': product.average_rating or 0,
                'review_count': product.review_count or 0,
                # 'variants': VariantSerializer(product_variants, many=True).data,
                # 'colors': list(product_colors),  # ensure list is serialized correctly
            }
            products_with_details.append(product_data)

        # Get and serialize banner data
        brand_data = BrandSerializer(top_brands, many=True, context={'request': request}).data

        # Get and serialize subcategory data
        subcategories = Sub_Category.objects.order_by('-engagement_score')[:4]
        subcategory_data = SubCategorySerializer(subcategories, many=True, context={'request': request}).data
        
        context = {
            "new_products": products_with_details,
            "most_popular": ProductSerializer(most_popular, many=True, context={'request': request}).data,
            "brands": brand_data,
            "subcategories": subcategory_data,
            "category": serializer.data,
        }

        return Response(context, status=status.HTTP_200_OK)



class RecentlyViewedRelatedProductsAPIView(APIView):
    def get(self, request):
        cookie_data = request.headers.get('X-Recently-Viewed')
        if not cookie_data:
            return Response([], status=status.HTTP_200_OK)

        try:
            # Handle both JSON and plain CSV formats
            if cookie_data.strip().startswith("["):
                product_ids = json.loads(cookie_data)
            else:
                product_ids = [int(x) for x in cookie_data.split(",") if x.strip().isdigit()]

            position = int(request.query_params.get("position", 0))
            product_id = product_ids[position]

            product = Product.objects.select_related("sub_category").get(pk=product_id)
            sub_category = product.sub_category

            if not sub_category:
                return Response([], status=status.HTTP_200_OK)

            related_products = Product.objects.filter(
                sub_category=sub_category,
                status='published'
            ).exclude(id=product.id)[:10]

            serializer = ProductSerializer(related_products, many=True, context={'request': request})
            return Response(serializer.data)

        except Exception as e:
            print("Error parsing cookie or accessing product:", e)
            return Response([], status=status.HTTP_200_OK)


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
        # 1. Retrieve Recently Viewed
        # -----------------------------
        try:
            viewed_cookie = request.COOKIES.get('recently_viewed', '[]')
            viewed_product_ids = json.loads(viewed_cookie)
            # Ensure it's a list of integers
            viewed_product_ids = [int(pid) for pid in viewed_product_ids if str(pid).isdigit()]
        except Exception:
            viewed_product_ids = []

        viewed_products_qs = Product.objects.filter(id__in=viewed_product_ids, status='published')
        products_dict = {product.id: product for product in viewed_products_qs}
        sorted_viewed_products = [products_dict[pid] for pid in viewed_product_ids if pid in products_dict]

        # -----------------------------
        # 2. Related by Category
        # -----------------------------
        related_products = set()
        for product in viewed_products_qs:
            related_products.update(
                Product.objects.filter(
                    status='published',
                    sub_category=product.sub_category
                ).exclude(id=product.id)
            )

        # -----------------------------
        # 3. Related by Search History
        # -----------------------------
        try:
            search_cookie = request.COOKIES.get('search_history', '[]')
            search_history = json.loads(search_cookie)
        except Exception:
            search_history = []

        search_related_products = set()
        for query in search_history:
            matched_by_title = Product.objects.filter(
                status="published", title__icontains=query
            ).exclude(id__in=viewed_product_ids)

            matched_by_description = Product.objects.filter(
                status="published", description__icontains=query
            ).exclude(id__in=viewed_product_ids)

            search_related_products.update(matched_by_title)
            search_related_products.update(matched_by_description)

        # -----------------------------
        # 4. Combine, Shuffle, Limit
        # -----------------------------
        combined_related = list(related_products | search_related_products)
        random.shuffle(combined_related)
        recommending_products = combined_related[:10]

        if not sorted_viewed_products and not search_history:
            recommending_products = Product.objects.filter(status='published').order_by('-views')[:10]

        # -----------------------------
        # 5. Serialize & Return
        # -----------------------------
        serialized_viewed = ProductSerializer(
            sorted_viewed_products, many=True, context={'request': request}
        ).data

        serialized_recommended = ProductSerializer(
            recommending_products, many=True, context={'request': request}
        ).data

        

        return Response({
            'recently_viewed': serialized_viewed,
            'recommended_products': serialized_recommended
        })
    

class TrendingProductsAPIView(APIView):
    def get(self, request):
        products_data = cache.get("top_trending_product")

        if not products_data:
            products = Product.objects.filter(status='published').order_by('-trending_score')[:10]
            # Serialize in base currency only (GHS)
            products_data = ProductSerializer(products, many=True, context={'request': request}).data
            cache.set("top_trending_products", products_data, timeout=600)  # Cache for 10 minutes

        # Always convert prices for current request currency (dynamic part)
        currency = request.headers.get('X-Currency', 'GHS')

        for product in products_data:
            product['currency'] = currency
            product['price'] = round(product['price'], 2)

        return Response(products_data)

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



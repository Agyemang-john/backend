from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from .models import *
from order.models import *
from .serializers import *
from django.db.models import Avg, Count, Q, Max, Min
from address.serializers import AddressSerializer
from django.http import Http404
from django.core.cache import cache
from rest_framework.permissions import AllowAny
from product.service import get_recommended_products, get_cart_based_recommendations, get_cart_product_ids
from rest_framework import status
from rest_framework.views import APIView
from decimal import Decimal
from order.service import *
from .service import get_fbt_recommendations
# from .shipping import can_product_ship_to_user
from copy import deepcopy
from rest_framework.permissions import IsAuthenticated
from django.db.models import F
from rest_framework.pagination import PageNumberPagination

from .utils import get_recently_viewed_products
from .tasks import increment_product_view_count

class AddProductReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        product_id = request.data.get('product')
        product = get_object_or_404(Product, id=product_id)

        if not self.user_has_purchased_product(request.user, product.id):
            return Response(
                {'detail': 'You must purchase the product before reviewing it.'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = ProductReviewSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save() 
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def user_has_purchased_product(self, user, product_id):
        return OrderProduct.objects.filter(
            order__user=user,
            product_id=product_id,
            order__is_ordered=True,
            order__status="delivered",
        ).exists()

class SitemapDataAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Fetch data
        products = Product.published.all().order_by('-trending_score')
        categories = Category.objects.all().order_by('-engagement_score')
        sub_categories = Sub_Category.objects.all().order_by('-engagement_score')
        brands = Brand.objects.all().order_by('-engagement_score')
        vendors = Vendor.objects.filter(is_approved=True).order_by('-views')

        # Serialize
        serialized_products = ProductSerializer(products, many=True, context={'request': request}).data
        serialized_categories = CategorySerializer(categories, many=True, context={'request': request}).data
        serialized_sub_categories = SubCategorySerializer(sub_categories, many=True, context={'request': request}).data
        serialized_brands = BrandSerializer(brands, many=True, context={'request': request}).data
        serialized_vendors = VendorSerializer(vendors, many=True, context={'request': request}).data

        # Return everything together
        return Response({
            "products": serialized_products,
            "categories": serialized_categories,
            "sub_categories": serialized_sub_categories,
            "brands": serialized_brands,
            "vendors": serialized_vendors,
        })
    

# class AjaxColorAPIView(APIView):
#     def post(self, request, *args, **kwargs):
#         size_id = request.data.get('size')
#         product_id = request.data.get('productid')
        
#         # Fetch the product by ID
#         product = get_object_or_404(Product, id=product_id)
        
#         # Fetch variants based on product ID and size ID
#         colors = Variants.objects.filter(product_id=product_id, size_id=size_id)

#         # Serialize the product and variants data
#         product_data = ProductSerializer(product, context={'request': request}).data
#         colors_data = VariantSerializer(colors, many=True, context={'request': request}).data
        
#         # Prepare the response data
#         response_data = {
#             'product': product_data,
#             'colors': colors_data
#         }
        
#         # Return the JSON response
#         return Response(response_data, status=status.HTTP_200_OK)


from .models import Product
from .serializers import ProductSerializer


class ProductRecommendationsAPIView(APIView):
    def get(self, request, sku, slug):
        # Step 1: Get only the IDs we need — no select_related conflict!
        product = get_object_or_404(
            Product.published.only('id', 'sub_category_id', 'vendor_id'),
            sku=sku,
            slug=slug
        )

        # Step 2: Build base queryset WITHOUT select_related on the main product query
        # We use .only() + select_related only on the final slices
        base_qs = Product.published.exclude(id=product.id)

        # Related products (same sub_category)
        related_products = list(
            base_qs.filter(sub_category_id=product.sub_category_id)
                   .select_related('vendor', 'sub_category')
                   .only(
                       'id', 'title', 'sku', 'slug', 'price', 'old_price', 'image',
                       'vendor__name', 'vendor__slug', 'sub_category__slug'
                   )[:12]
        )

        # Vendor products (same vendor)
        vendor_products = list(
            base_qs.filter(vendor_id=product.vendor_id)
                   .select_related('vendor')
                   .only(
                       'id', 'title', 'sku', 'slug', 'price', 'old_price', 'image',
                       'vendor__name', 'vendor__slug'
                   )[:12]
        )

        # Serialize properly
        serializer = ProductSerializer(many=True, context={'request': request})

        data = {
            "related_products": serializer.to_representation(related_products),
            "vendor_products": serializer.to_representation(vendor_products),
        }

        return Response(data)

from django.db.models import Prefetch
def get_cached_product_data(sku: str, slug: str, request):
    """
    Fast cached product detail data — NO related/vendor products
    """
    cache_key = f"product_detail_v2:{sku}:{slug}"
    cached = cache.get(cache_key)

    if cached:
        # Still return fresh product instance for view count logic
        product = Product.objects.only('id').get(sku=sku, slug=slug)
        return deepcopy(cached), product

    # Main product with optimized prefetching
    product = get_object_or_404(
        Product.published
        .select_related('vendor', 'sub_category')
        .prefetch_related(
            Prefetch('p_images', queryset=ProductImages.objects.order_by('id')),
            Prefetch('reviews', queryset=ProductReview.objects.filter(status=True))
        )
        .annotate(
            average_rating=Avg('reviews__rating'),
            review_count=Count('reviews')
        ),
        sku=sku,
        slug=slug
    )

    # Serialize efficiently
    shared_data = {
        "product": ProductSerializer(product, context={'request': request}).data,
        "p_images": ProductImageSerializer(
            product.p_images.all(), many=True, context={'request': request}
        ).data,
        "reviews": ProductReviewSerializer(
            product.reviews.filter(status=True), many=True, context={'request': request}
        ).data,
        "average_rating": product.average_rating or 0,
        "review_count": product.review_count or 0,
        "delivery_options": ProductDeliveryOptionSerializer(
            ProductDeliveryOption.objects.filter(product=product), many=True
        ).data,
    }

    # Remove price fields — will be added dynamically per currency
    for field in ['price', 'old_price', 'currency']:
        shared_data['product'].pop(field, None)

    # Cache for 30 minutes
    cache.set(cache_key, shared_data, timeout=60 * 30)
    return deepcopy(shared_data), product


def convert_currency(product_data: dict, currency: str) -> dict:
    """
    Convert prices for main product + variant only
    """
    rates = get_exchange_rates()
    exchange_rate = Decimal(str(rates.get(currency, 1)))

    # Main product
    main_product = product_data['product']
    try:
        db_product = Product.published.only('price', 'old_price').get(id=main_product['id'])
        main_product.update({
            'price': round(db_product.price * exchange_rate, 2),
            'old_price': round(db_product.old_price * exchange_rate, 2) if db_product.old_price else None,
            'currency': currency
        })
    except Product.DoesNotExist:
        pass

    # Variant (if exists)
    if 'variant_data' in product_data and product_data['variant_data'].get('variant'):
        variant_info = product_data['variant_data']['variant']
        try:
            variant = Variants.objects.only('price').get(id=variant_info['id'])
            variant_info['price'] = round(variant.price * exchange_rate, 2)
            variant_info['currency'] = currency
        except Variants.DoesNotExist:
            pass

    return product_data


class ProductDetailAPIView(APIView):
    def get(self, request, sku, slug):
        try:
            variant_id = request.GET.get('variantid')
            currency = request.headers.get('X-Currency', 'GHS')
            
            try:
                shared_data, product = get_cached_product_data(sku, slug, request)
            except Http404:
                return Response({"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND)
            
            product_id_str = str(product.id)
            recently_viewed = request.session.get('recently_viewed', [])
            recently_viewed = [str(pid) for pid in recently_viewed]

            if product_id_str in recently_viewed:
                recently_viewed.remove(product_id_str)
            recently_viewed.insert(0, product_id_str)
            request.session['recently_viewed'] = recently_viewed[:10]

            # =====================================================
            # 2. PERMANENT VIEW DEDUPLICATION (never cleared by user)
            # =====================================================
            viewed_for_count = request.session.get('viewed_for_count', set())
            if product_id_str not in viewed_for_count:
                # First real view → mark it and increment ASYNC
                viewed_for_count.add(product_id_str)
                request.session['viewed_for_count'] = viewed_for_count
                increment_product_view_count.delay(product.id)
            request.session.modified = True

            # Optimize variant queries
            variant = None
            if variant_id:
                variant = Variants.objects.filter(id=variant_id, product=product).first()
            if not variant:
                variant = Variants.objects.filter(product=product).first()
            
            stock_quantity = product.get_stock_quantity(variant)
            is_out_of_stock = stock_quantity <= 0

            # can_ship, user_region = can_product_ship_to_user(request, product)

            variant_data = {}
            if product.variant != "None" and variant:
                variants = Variants.objects.filter(product=product).select_related(
                    "size", "color"
                ).prefetch_related("variantimage_set")

                size_variant_ids = (
                    variants.values("size")
                    .annotate(min_id=Min("id"))
                    .values_list("min_id", flat=True)
                )
                size_variants = variants.filter(id__in=size_variant_ids)
                same_size_variants = variants.filter(size_id=variant.size_id).distinct("color_id")

                variant_data = {
                    "variant": VariantSerializer(variant, context={"request": request}).data,
                    "variant_images": VariantImageSerializer(
                        variant.variantimage_set.all(), many=True, context={"request": request}
                    ).data,
                    "colors": VariantSerializer(same_size_variants, many=True, context={"request": request}).data,
                    "sizes": VariantSerializer(size_variants, many=True, context={"request": request}).data,
                }
            
            shared_data['variant_data'] = variant_data
            shared_data = convert_currency(shared_data, currency)

            # Optimize follow check
            is_following = False
            follower_count = 0
            if request.user.is_authenticated:
                is_following = product.vendor.followers.filter(id=request.user.id).exists()
                follower_count = product.vendor.followers.count()

            # Optimize address query
            address = None
            if request.user.is_authenticated:
                address = Address.objects.filter(user=request.user, status=True).first()

            # Optimize cart data retrieval
            cart_data = self._get_cart_data(request, product, variant)

            # if cart_data["cart_quantity"] >= stock_quantity and stock_quantity != 0:
            #     is_out_of_stock = True
            
            response_data = {
                **shared_data,
                "address": AddressSerializer(address).data if address else None,
                "is_out_of_stock": is_out_of_stock,
                "available_stock": stock_quantity,
                "is_in_cart": cart_data["is_in_cart"],
                "cart_quantity": cart_data["cart_quantity"],
                "cart_item_id": cart_data["cart_item_id"],
                'is_following': is_following,
                'follower_count': follower_count,
                "user_region": None,
                "can_ship": True
            }

            # Create the response object
            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {"error": "Failed to load product data", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _get_cart_data(self, request, product, variant):
        cart_data = {
            'is_in_cart': False,
            'cart_quantity': 0,
            'cart_item_id': None
        }
        item_key = f"{product.id}_{variant.id if variant else 'none'}"

        if request.user.is_authenticated:
            try:
                cart = Cart.objects.get(user=request.user)
                cart_item = CartItem.objects.filter(
                    cart=cart, product=product, variant=variant
                ).only('id', 'quantity').first()

                if cart_item:
                    cart_data.update({
                        'is_in_cart': True,
                        'cart_quantity': cart_item.quantity,
                        'cart_item_id': cart_item.id
                    })
            except Cart.DoesNotExist:
                pass
        else:
            guest_cart = request.session.get("guest_cart", {})
            quantity = guest_cart.get(item_key, 0)
            if quantity > 0:
                cart_data.update({
                    'is_in_cart': True,
                    'cart_quantity': quantity
                })

        return cart_data



class SearchSuggestionsAPIView(APIView):
    def get(self, request, *args, **kwargs):
        query = request.GET.get("q", "").strip()

        if not query:
            return Response([], status=status.HTTP_200_OK)

        # 🔑 Use lowercase cache key per query
        cache_key = f"search_suggestions:{query.lower()}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(cached_data, status=status.HTTP_200_OK)

        search_query = SearchQuery(query, search_type="plain")  # could also use 'phrase' or 'websearch'

        suggestions_qs = (
            Product.objects.filter(status="published")
            .annotate(rank=SearchRank(F("search_vector"), search_query))
            .filter(rank__gt=0.0)
            .select_related("sub_category")
            .order_by("-rank", "title")[:10]
        )

        suggestions = [
            {
                "title": product.title,
                "price": product.price,
                "sku": product.sku,
                "slug": product.slug,
                "thumbnail": request.build_absolute_uri(product.image.url)
                if product.image
                else None,
                "category": product.sub_category.title if product.sub_category else "Uncategorized",
            }
            for product in suggestions_qs
        ]

        cache.set(cache_key, suggestions, timeout=600)

        return Response(suggestions, status=status.HTTP_200_OK)

class CategoryProductListView(APIView):
    def get(self, request, slug):
        # Fetch the category by slug
        category = Sub_Category.objects.filter(slug=slug).first()
        if not category:
            return Response({"detail": "Category not found"}, status=404)
        
        # Extract currency and exchange rate
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        # Base queryset for the category (before filters)
        base_queryset = Product.objects.filter(
            status="published",
            sub_category=category
        ).annotate(
            average_rating=Avg('reviews__rating'),
            review_count=Count('reviews')
        ).order_by('id')  # To prevent UnorderedObjectListWarning

        # Unfiltered price range for slider bounds
        unfiltered_price_range = base_queryset.aggregate(
            min_price_unfiltered=Min('price'),
            max_price_unfiltered=Max('price')
        )
        min_price_unfiltered = unfiltered_price_range['min_price_unfiltered'] or 0
        max_price_unfiltered = unfiltered_price_range['max_price_unfiltered'] or 0

        # Initialize filters
        try:
            active_colors = [int(i) for i in request.GET.getlist('color') if i.isdigit()]
            active_sizes = [int(i) for i in request.GET.getlist('size') if i.isdigit()]
            active_brands = [int(i) for i in request.GET.getlist('brand') if i.isdigit()]
            active_vendors = [int(i) for i in request.GET.getlist('vendor') if i.isdigit()]
            rating = [int(i) for i in request.GET.getlist('rating') if i.isdigit()]
            min_price = Decimal(request.GET.get('from')) if request.GET.get('from') else None
            max_price = Decimal(request.GET.get('to')) if request.GET.get('to') else None
        except ValueError:
            return Response({"detail": "Invalid filter parameters"}, status=400)

        # Apply filters to base queryset
        filtered_products = base_queryset
        filters = Q()

        if active_colors:
            filters &= Q(variants__color__id__in=active_colors)
        if active_sizes:
            filters &= Q(variants__size__id__in=active_sizes)
        if active_brands:
            filters &= Q(brand__id__in=active_brands)
        if active_vendors:
            filters &= Q(vendor__id__in=active_vendors)
        if min_price is not None:
            filters &= Q(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filters &= Q(price__lte=max_price / exchange_rate)
        if rating:
            filters &= Q(average_rating__gte=min(rating))

        if filters:
            filtered_products = base_queryset.filter(filters).distinct().annotate(
                average_rating=Avg('reviews__rating'),
                review_count=Count('reviews')
            )

        # Price range based on filtered products
        price_range = filtered_products.aggregate(
            max_price=Max('price'), min_price=Min('price')
        )

        # Pagination
        paginator = PageNumberPagination()
        paginator.page_size = 12
        try:
            total_items = filtered_products.count()
            total_pages = max(1, (total_items + paginator.page_size - 1) // paginator.page_size)
            requested_page = int(request.GET.get('page', '1'))
            if requested_page > total_pages or total_items == 0:
                requested_page = 1
            request._request.GET._mutable = True
            request._request.GET['page'] = str(requested_page)
            request._request.GET._mutable = False
            paged_products = paginator.paginate_queryset(filtered_products, request)
        except Exception:
            paged_products = []
            paginator._page_number = 1
            paginator.page = None

        # Serialize paginated products
        serialized_products = ProductSerializer(paged_products, many=True, context={'request': request}).data

        # Prepare product details
        products_with_details = []
        for product in paged_products or []:
            product_variants = Variants.objects.filter(product=product)
            product_colors = product_variants.values('color__name', 'color__code', 'id').distinct()
            products_with_details.append({
                'product': ProductSerializer(product, context={'request': request}).data,
                'average_rating': product.average_rating or 0,
                'review_count': product.review_count or 0,
                'variants': VariantSerializer(product_variants, many=True).data,
                'colors': list(product_colors),
            })

        # Sidebar filters
        sizes = Size.objects.filter(variants__product__sub_category=category).distinct()
        colors = Color.objects.filter(variants__product__sub_category=category).distinct()
        brands = Brand.objects.filter(product__sub_category=category).distinct()
        vendors = Vendor.objects.filter(product__sub_category=category).distinct()

        context = {
            "colors": ColorSerializer(colors, many=True).data,
            "sizes": SizeSerializer(sizes, many=True).data,
            "vendors": VendorSerializer(vendors, many=True).data,
            "brands": BrandSerializer(brands, many=True).data,
            "category": SubCategorySerializer(category).data,
            "products": serialized_products,
            "products_with_details": products_with_details,
            "min_price": round((price_range['min_price'] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((price_range['max_price'] or max_price_unfiltered) * exchange_rate, 2),
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),
            "default_max_price": round(10000 * exchange_rate, 2),
            "currency": currency,
            "next": paginator.get_next_link() if paged_products else None,
            "previous": paginator.get_previous_link() if paged_products else None,
            "total": total_items,
        }
        return Response(context)


class BrandProductListView(APIView):
    def get(self, request, slug):
        # Fetch the brand by slug
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))
        
        brand = Brand.objects.filter(slug=slug).first()
        if not brand:
            return Response({"detail": "Brand not found"}, status=404)
        
        # Base queryset for the brand (before filters)
        base_queryset = Product.objects.filter(
            status="published",
            brand=brand
        ).annotate(
            average_rating=Avg('reviews__rating'),
            review_count=Count('reviews')
        ).order_by('id')  # To prevent UnorderedObjectListWarning

        # Unfiltered price range for slider bounds
        unfiltered_price_range = base_queryset.aggregate(
            min_price_unfiltered=Min('price'),
            max_price_unfiltered=Max('price')
        )
        min_price_unfiltered = unfiltered_price_range['min_price_unfiltered'] or 0
        max_price_unfiltered = unfiltered_price_range['max_price_unfiltered'] or 0

        converted_min_unfiltered = round(min_price_unfiltered * exchange_rate, 2)
        converted_max_unfiltered = round(max_price_unfiltered * exchange_rate, 2)


        # Initialize filters
        try:
            active_colors = [int(i) for i in request.GET.getlist('color') if i.isdigit()]
            active_sizes = [int(i) for i in request.GET.getlist('size') if i.isdigit()]
            active_vendors = [int(i) for i in request.GET.getlist('vendor') if i.isdigit()]
            rating = [int(i) for i in request.GET.getlist('rating') if i.isdigit()]
            min_price = Decimal(request.GET.get('from')) if request.GET.get('from') else None
            max_price = Decimal(request.GET.get('to')) if request.GET.get('to') else None
        except ValueError:
            return Response({"detail": "Invalid filter parameters"}, status=400)

        # Apply filters to base queryset
        filtered_products = base_queryset
        filters = Q()

        if active_colors:
            filters &= Q(variants__color__id__in=active_colors)
        if active_sizes:
            filters &= Q(variants__size__id__in=active_sizes)
        if active_vendors:
            filters &= Q(vendor__id__in=active_vendors)
        if min_price is not None:
            filters &= Q(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filters &= Q(price__lte=max_price / exchange_rate)
        if rating:
            filters &= Q(average_rating__gte=min(rating))

        if filters:
            filtered_products = base_queryset.filter(filters).distinct().annotate(
                average_rating=Avg('reviews__rating'),
                review_count=Count('reviews')
            )

        # Price range based on filtered products
        price_range = filtered_products.aggregate(
            max_price=Max('price'), min_price=Min('price')
        )

        converted_min_price = round((price_range['min_price'] or min_price_unfiltered) * exchange_rate, 2)
        converted_max_price = round((price_range['max_price'] or max_price_unfiltered) * exchange_rate, 2)


        # Pagination
        paginator = PageNumberPagination()
        paginator.page_size = 12
        try:
            total_items = filtered_products.count()
            total_pages = max(1, (total_items + paginator.page_size - 1) // paginator.page_size)
            requested_page = int(request.GET.get('page', '1'))
            if requested_page > total_pages or total_items == 0:
                requested_page = 1
            request._request.GET._mutable = True
            request._request.GET['page'] = str(requested_page)
            request._request.GET._mutable = False
            paged_products = paginator.paginate_queryset(filtered_products, request)
        except Exception:
            paged_products = []
            paginator._page_number = 1
            paginator.page = None

        # Serialize paginated products
        serialized_products = ProductSerializer(paged_products, many=True, context={'request': request}).data

        # Prepare product details
        products_with_details = []
        for product in paged_products or []:
            product_variants = Variants.objects.filter(product=product)
            product_colors = product_variants.values('color__name', 'color__code', 'id').distinct()
            products_with_details.append({
                'product': ProductSerializer(product, context={'request': request}).data,
                'average_rating': product.average_rating or 0,
                'review_count': product.review_count or 0,
                'variants': VariantSerializer(product_variants, many=True).data,
                'colors': list(product_colors),
            })

        # Sidebar filters
        sizes = Size.objects.filter(variants__product__brand=brand).distinct()
        colors = Color.objects.filter(variants__product__brand=brand).distinct()
        vendors = Vendor.objects.filter(product__brand=brand).distinct()

        context = {
            "colors": ColorSerializer(colors, many=True).data,
            "sizes": SizeSerializer(sizes, many=True).data,
            "vendors": VendorSerializer(vendors, many=True).data,
            "brand": BrandSerializer(brand).data,
            "products": serialized_products,
            "products_with_details": products_with_details,
            "min_price": converted_min_price,
            "max_price": converted_max_price,
            "min_price_unfiltered": converted_min_unfiltered,
            "max_price_unfiltered": converted_max_unfiltered,
            "currency": currency,
            "exchange_rate": exchange_rate,
            "default_max_price": round(10000 * exchange_rate,2),
            "next": paginator.get_next_link() if paged_products else None,
            "previous": paginator.get_previous_link() if paged_products else None,
            "total": total_items,
        }
        return Response(context)

# from elasticsearch8 import Elasticsearch

import logging

# Configure logging
logger = logging.getLogger(__name__)
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank

class ProductSearchAPIView(APIView):

    def get(self, request, format=None):
        query = request.GET.get('q', '').strip()

        base_queryset = (
            Product.objects.filter(title__icontains=query, status="published")
            .annotate(average_rating=Avg('reviews__rating'), review_count=Count('reviews'))
            .select_related("brand", "vendor", "sub_category")
            .prefetch_related("variants__color", "variants__size", "reviews")
            .order_by("id")
        )

        # Currency setup
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()  # Assuming this function exists
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        # Get unfiltered price range for slider bounds
        unfiltered_price_range = base_queryset.aggregate(
            min_price_unfiltered=Min('price'),
            max_price_unfiltered=Max('price')
        )
        min_price_unfiltered = unfiltered_price_range['min_price_unfiltered'] or 0
        max_price_unfiltered = unfiltered_price_range['max_price_unfiltered'] or 0

        # Initialize filters
        try:
            active_colors = [int(i) for i in request.GET.getlist('color') if i.isdigit()]
            active_sizes = [int(i) for i in request.GET.getlist('size') if i.isdigit()]
            active_brands = [int(i) for i in request.GET.getlist('brand') if i.isdigit()]
            active_vendors = [int(i) for i in request.GET.getlist('vendor') if i.isdigit()]
            rating = [int(i) for i in request.GET.getlist('rating') if i.isdigit()]
            min_price = Decimal(request.GET.get('from')) if request.GET.get('from') else None
            max_price = Decimal(request.GET.get('to')) if request.GET.get('to') else None
        except ValueError:
            return Response({"detail": "Invalid filter parameters"}, status=400)

        # Apply filters to base queryset
        filters = Q()
        if active_colors:
            filters &= Q(variants__color__id__in=active_colors)
        if active_sizes:
            filters &= Q(variants__size__id__in=active_sizes)
        if active_brands:
            filters &= Q(brand__id__in=active_brands)
        if active_vendors:
            filters &= Q(vendor__id__in=active_vendors)
        if min_price is not None:
            filters &= Q(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filters &= Q(price__lte=max_price / exchange_rate)
        if rating:
            filters &= Q(average_rating__gte=min(rating))

        # === PostgreSQL Full-Text Search ===
        if query:
            try:
                search_query = SearchQuery(query, config='english')
                base_queryset = base_queryset.annotate(
                    rank=SearchRank(
                        SearchVector('title', weight='A') +
                        SearchVector('description', weight='B') +
                        SearchVector('features', weight='C') +
                        SearchVector('specifications', weight='C'),
                        search_query
                    )
                ).filter(search_vector=search_query).order_by('-rank')
            except Exception as e:
                logger.error(f"PostgreSQL search error: {str(e)}")
                # Fallback to title-based search if full-text search fails
                base_queryset = base_queryset.filter(title__icontains=query)

        # Apply all filters
        filtered_products = base_queryset.filter(filters).distinct()

        # Price range based on filtered products
        price_range = filtered_products.aggregate(
            max_price=Max('price'),
            min_price=Min('price')
        )

        # Pagination
        paginator = PageNumberPagination()
        paginator.page_size = 12
        try:
            total_items = filtered_products.count()
            total_pages = max(1, (total_items + paginator.page_size - 1) // paginator.page_size)
            requested_page = int(request.GET.get('page', '1'))
            if requested_page > total_pages or total_items == 0:
                requested_page = 1
            request._request.GET._mutable = True
            request._request.GET['page'] = str(requested_page)
            request._request.GET._mutable = False
            paged_products = paginator.paginate_queryset(filtered_products, request)
        except Exception:
            paged_products = []
            paginator._page_number = 1
            paginator.page = None

        # Serialize paginated products
        serialized_products = ProductSerializer(paged_products, many=True, context={'request': request}).data

        # Prepare product details
        products_with_details = []
        for product in paged_products or []:
            product_variants = Variants.objects.filter(product=product)
            product_colors = product_variants.values('color__name', 'color__code', 'id').distinct()
            products_with_details.append({
                'product': ProductSerializer(product, context={'request': request}).data,
                'average_rating': product.average_rating or 0,
                'review_count': product.review_count or 0,
                'variants': VariantSerializer(product_variants, many=True).data,
                'colors': list(product_colors),
            })

        # Sidebar filters
        sizes = Size.objects.filter(variants__product__in=filtered_products).distinct()
        colors = Color.objects.filter(variants__product__in=filtered_products).distinct()
        brands = Brand.objects.filter(product__in=filtered_products).distinct()
        vendors = Vendor.objects.filter(product__in=filtered_products).distinct()
        categories = Sub_Category.objects.filter(product__in=filtered_products).distinct()

        context = {
            "colors": ColorSerializer(colors, many=True).data,
            "sizes": SizeSerializer(sizes, many=True).data,
            "vendors": VendorSerializer(vendors, many=True).data,
            "brands": BrandSerializer(brands, many=True).data,
            "categories": SubCategorySerializer(categories, many=True).data,
            "products": serialized_products,
            "products_with_details": products_with_details,
            "min_price": round((price_range['min_price'] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((price_range['max_price'] or max_price_unfiltered) * exchange_rate, 2),
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),
            "default_max_price": round(10000 * exchange_rate, 2),
            "currency": currency,
            "next": paginator.get_next_link() if paged_products else None,
            "previous": paginator.get_previous_link() if paged_products else None,
            "total": total_items,
        }
        return Response(context)
         

class RecentlyViewedProducts(APIView):
    def get(self, request):
        products = get_recently_viewed_products(request, limit=10)

        # Optional: If you want to return empty list instead of 404
        if not products.exists():
            return Response([], status=status.HTTP_200_OK)

        serializer = ProductSerializer(
            products,
            many=True,
            context={'request': request}
        )
        return Response(serializer.data)

class ClearRecentlyViewed(APIView):
    http_method_names = ['post']
    def post(self, request):
        if 'recently_viewed' in request.session:
            del request.session['recently_viewed']
            request.session.modified = True
        return Response({"message": "Recently viewed cleared"}, status=status.HTTP_200_OK)


class RemoveRecentlyViewedItem(APIView):
    http_method_names = ['post']
    
    def post(self, request):
        product_id = request.data.get('product_id')
        if not product_id:
            return Response({"error": "product_id required"}, status=status.HTTP_400_BAD_REQUEST)

        recently_viewed = request.session.get('recently_viewed', [])
        pid_str = str(product_id)
        if pid_str in recently_viewed:
            recently_viewed.remove(pid_str)
            request.session['recently_viewed'] = recently_viewed
            request.session.modified = True

        return Response({"message": "Item removed", "removed": pid_str})


class CartRecommendationsAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Get current cart product IDs — works for guest (session) AND logged-in (DB)
        cart_product_ids = get_cart_product_ids(request)

        # 1. Frequently Bought Together (based on cart co-occurrence)
        bought_together_set = set()
        for pid in cart_product_ids:
            related = get_cart_based_recommendations(pid)
            bought_together_set.update(related.values_list('id', flat=True))

        bought_together = Product.objects.filter(
            id__in=bought_together_set
        ).exclude(id__in=cart_product_ids)[:10]

        # 2. Personalized Recommendations (category, FBT, trending)
        personalized = get_recommended_products(request)

        return Response({
            "frequently_bought_together": ProductSerializer(
                bought_together,
                many=True,
                context={'request': request}
            ).data,
            "recommended_for_you": ProductSerializer(
                personalized,
                many=True,
                context={'request': request}
            ).data,
        }, status=status.HTTP_200_OK)
    

class FrequentlyBoughtTogetherAPIView(APIView):

    def get(self, request):
        # Step 1: Get the cart for the current request
        cart = Cart.objects.get_for_request(request)
        if not cart:
            return Response([], status=200)

        # Step 2: Extract product IDs from CartItems
        cart_items = CartItem.objects.filter(cart=cart).select_related('product')
        cart_product_ids = [item.product.id for item in cart_items if item.product]

        if not cart_product_ids:
            return Response([], status=200)

        # Step 3: Get FBT recommendations
        related_products = get_fbt_recommendations(cart_product_ids)

        # Step 4: Serialize and return
        serializer = ProductSerializer(related_products, many=True, context={'request': request})
        return Response(serializer.data, status=200)

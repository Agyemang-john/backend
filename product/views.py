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
from rest_framework.permissions import IsAuthenticated
from django.db.models import F
from rest_framework.pagination import PageNumberPagination

from .utils import get_recently_viewed_products, update_recently_viewed
from .tasks import increment_product_view_count
# from .shipping import can_product_ship_to_user

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
        return cached, product

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
    cache.set(cache_key, shared_data, timeout=60 * 60)
    return shared_data, product

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
            
            update_recently_viewed(request.session, product.id)

            product_id_str = str(product.id)
            viewed_for_count = request.session.get('viewed_for_count', set())
            if product_id_str not in viewed_for_count:
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
    """
    Returns all products for a given sub-category, with filtering and pagination.

    FILTERING STRATEGY (same pattern as BrandProductListView):
    1. Fetch ALL published products for the category (base queryset).
    2. Derive sidebar filter options from this base set (cached 1 hour).
    3. Apply user-selected filters on top of the base to get the display set.

    The sidebar always reflects the full category catalog so users can
    freely combine/remove filters without losing visibility of options.

    PERFORMANCE:
    - Category object cached for 1 hour.
    - Unfiltered price range cached for 1 hour.
    - Sidebar filter options cached for 1 hour.
    - select_related / prefetch_related / only() to minimize DB hits.
    - .distinct() only applied when variant joins could produce duplicates.
    """

    def get(self, request, slug):
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1: Fetch Category (cached for 1 hour)
        # ═══════════════════════════════════════════════════════════════════════
        cache_key = f"category:{slug}"
        category = cache.get(cache_key)

        if not category:
            category = Sub_Category.objects.filter(slug=slug).first()
            if not category:
                return Response({"detail": "Category not found"}, status=404)
            cache.set(cache_key, category, 3600)

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2: Currency setup & parse filter parameters from query string
        # ═══════════════════════════════════════════════════════════════════════
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        try:
            active_colors = [int(i) for i in request.GET.getlist('color') if i.isdigit()]
            active_sizes = [int(i) for i in request.GET.getlist('size') if i.isdigit()]
            active_brands = [int(i) for i in request.GET.getlist('brand') if i.isdigit()]
            active_vendors = [int(i) for i in request.GET.getlist('vendor') if i.isdigit()]
            rating = [int(i) for i in request.GET.getlist('rating') if i.isdigit()]
            min_price = Decimal(request.GET.get('from')) if request.GET.get('from') else None
            max_price = Decimal(request.GET.get('to')) if request.GET.get('to') else None
            page = int(request.GET.get('page', 1))
        except (ValueError, TypeError):
            return Response({"detail": "Invalid filter parameters"}, status=400)

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 3: Unfiltered price range (cached for 1 hour)
        # Used for the price slider bounds — always spans the full category.
        # ═══════════════════════════════════════════════════════════════════════
        unfiltered_cache_key = f"price_range:{slug}"
        unfiltered_price_range = cache.get(unfiltered_cache_key)

        if not unfiltered_price_range:
            unfiltered_price_range = Product.objects.filter(
                status="published",
                sub_category=category
            ).aggregate(
                min_price_unfiltered=Min('price'),
                max_price_unfiltered=Max('price')
            )
            cache.set(unfiltered_cache_key, unfiltered_price_range, 3600)

        min_price_unfiltered = unfiltered_price_range.get('min_price_unfiltered') or Decimal('0')
        max_price_unfiltered = unfiltered_price_range.get('max_price_unfiltered') or Decimal('0')

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 4: Base queryset — ALL published products in this category.
        # This is the single source of truth. Sidebar filters and the
        # product list both derive from this queryset.
        # ═══════════════════════════════════════════════════════════════════════
        base_queryset = Product.objects.filter(
            status="published",
            sub_category=category
        ).select_related(
            'brand', 'vendor', 'sub_category'
        ).prefetch_related(
            Prefetch(
                'reviews',
                queryset=ProductReview.objects.filter(status=True).only('rating', 'product_id')
            ),
            Prefetch(
                'variants',
                queryset=Variants.objects.select_related('color', 'size').only(
                    'id', 'product_id', 'color__id', 'color__name', 'color__code',
                    'size__id', 'size__name', 'quantity', 'price'
                )
            )
        ).only(
            'id', 'title', 'slug', 'sku', 'image', 'price', 'old_price',
            'brand__id', 'brand__title', 'brand__slug',
            'vendor__id', 'vendor__name',
            'sub_category__id', 'sub_category__title'
        )

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 5: Apply user-selected filters on top of the base queryset.
        # Only adds .distinct() when variant joins (color/size) could
        # produce duplicate product rows.
        # ═══════════════════════════════════════════════════════════════════════
        filtered_products = base_queryset
        needs_distinct = False

        if active_colors:
            filtered_products = filtered_products.filter(variants__color__id__in=active_colors)
            needs_distinct = True
        if active_sizes:
            filtered_products = filtered_products.filter(variants__size__id__in=active_sizes)
            needs_distinct = True
        if active_brands:
            filtered_products = filtered_products.filter(brand__id__in=active_brands)
        if active_vendors:
            filtered_products = filtered_products.filter(vendor__id__in=active_vendors)
        if min_price is not None:
            filtered_products = filtered_products.filter(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filtered_products = filtered_products.filter(price__lte=max_price / exchange_rate)

        # Annotate AFTER filtering to avoid computing ratings for excluded products,
        # but BEFORE .distinct() so the aggregation is accurate.
        filtered_products = filtered_products.annotate(
            average_rating=Avg('reviews__rating'),
            review_count=Count('reviews', distinct=True)
        )

        if rating:
            filtered_products = filtered_products.filter(average_rating__gte=min(rating))

        if needs_distinct:
            filtered_products = filtered_products.distinct()

        # Consistent ordering for stable pagination
        filtered_products = filtered_products.order_by('id')

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 6: Filtered price range (reflects the narrowed-down set)
        # ═══════════════════════════════════════════════════════════════════════
        price_range = filtered_products.aggregate(
            max_price=Max('price'),
            min_price=Min('price')
        )

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 7: Manual pagination (faster than DRF paginator for this case)
        # Clamps the page number to valid bounds and slices the queryset.
        # ═══════════════════════════════════════════════════════════════════════
        PAGE_SIZE = 12

        try:
            total_items = filtered_products.count()
            total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)

            # Clamp page to valid range
            if page < 1 or (page > total_pages and total_items > 0):
                page = 1

            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            paged_products = list(filtered_products[start:end])

        except Exception as e:
            logger.error(f"Pagination error in CategoryProductListView: {e}")
            paged_products = []
            total_items = 0
            page = 1

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 8: Build product detail objects from prefetched data.
        # All variant/color data comes from the prefetch cache — no extra queries.
        # ═══════════════════════════════════════════════════════════════════════
        products_with_details = []

        for product in paged_products:
            product_variants = list(product.variants.all())

            # De-duplicate colors by color ID
            product_colors = {}
            for variant in product_variants:
                if variant.color and variant.color.id not in product_colors:
                    product_colors[variant.color.id] = {
                        'color__name': variant.color.name,
                        'color__code': variant.color.code,
                        'id': variant.id
                    }

            products_with_details.append({
                'product': ProductSerializer(product, context={'request': request}).data,
                'average_rating': float(product.average_rating) if product.average_rating else 0.0,
                'review_count': product.review_count or 0,
                'variants': VariantSerializer(product_variants, many=True).data,
                'colors': list(product_colors.values()),
            })

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 9: Sidebar Filter Options (from UNFILTERED base queryset)
        #
        # These are derived from ALL published products in this category,
        # NOT from the filtered results. This ensures the sidebar always
        # shows every available color/size/brand/vendor for the category,
        # so users can freely adjust filters without options disappearing.
        #
        # Cached for 1 hour to avoid repeated DB hits on every request.
        # Trade-off: newly added products won't appear in sidebar for up to 1 hour.
        # ═══════════════════════════════════════════════════════════════════════
        filter_cache_key = f"filters:{slug}"
        filter_options = cache.get(filter_cache_key)

        if not filter_options:
            sizes = list(Size.objects.filter(
                variants__product__in=base_queryset
            ).distinct().values('id', 'name'))

            colors = list(Color.objects.filter(
                variants__product__in=base_queryset
            ).distinct().values('id', 'name', 'code'))

            brands = list(Brand.objects.filter(
                product__in=base_queryset
            ).distinct().values('id', 'title'))

            vendors = list(Vendor.objects.filter(
                product__in=base_queryset
            ).distinct().values('id', 'name'))

            filter_options = {
                "colors": colors,
                "sizes": sizes,
                "vendors": vendors,
                "brands": brands,
            }
            cache.set(filter_cache_key, filter_options, 3600)

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 10: Build pagination URLs (preserves existing query params)
        # ═══════════════════════════════════════════════════════════════════════
        def build_pagination_url(page_num):
            if page_num < 1 or page_num > total_pages or total_items == 0:
                return None
            params = request.GET.copy()
            params['page'] = page_num
            return f"/api/v1/product/category/{slug}/?{params.urlencode()}"

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 11: Build and return the response
        # ═══════════════════════════════════════════════════════════════════════
        context = {
            # Sidebar options (from unfiltered base, cached)
            **filter_options,

            "category": SubCategorySerializer(category).data,

            # Product list (filtered + paginated)
            "products": [p['product'] for p in products_with_details],
            "products_with_details": products_with_details,

            # Price range after filters (for display)
            "min_price": round((price_range['min_price'] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((price_range['max_price'] or max_price_unfiltered) * exchange_rate, 2),

            # Price range before filters (for slider bounds)
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),
            "default_max_price": round(Decimal('10000') * exchange_rate, 2),

            "currency": currency,

            # Pagination
            "next": build_pagination_url(page + 1),
            "previous": build_pagination_url(page - 1) if page > 1 else None,
            "total": total_items,
            "current_page": page,
            "total_pages": total_pages,
        }

        return Response(context)

class BrandProductListView(APIView):
    """
    Returns all products for a given brand, with filtering and pagination.

    FILTERING STRATEGY:
    1. Fetch ALL published products for the brand (base queryset).
    2. Derive sidebar filter options (colors, sizes, vendors) from this base set.
    3. Apply user-selected filters on top of the base to get the display set.

    This means the sidebar always reflects the full brand catalog, so users
    can freely combine/remove filters without losing visibility of options.
    """

    def get(self, request, slug):
        # ─────────────────────────────────────────────
        # Currency conversion setup
        # ─────────────────────────────────────────────
        currency = request.headers.get("X-Currency", "GHS")
        exchange_rate = Decimal(str(get_exchange_rates().get(currency, 1)))

        # ─────────────────────────────────────────────
        # Fetch the brand by slug
        # ─────────────────────────────────────────────
        brand = Brand.objects.filter(slug=slug).first()
        if not brand:
            return Response({"detail": "Brand not found"}, status=404)

        # ─────────────────────────────────────────────
        # Base queryset: ALL published products for this brand.
        # This is the single source of truth for both the product list
        # and the sidebar filter options. Annotated once to avoid
        # repeated subqueries for rating/review count.
        # ─────────────────────────────────────────────
        base_products = (
            Product.objects
            .filter(status="published", brand=brand)
            .select_related("vendor", "brand")
            .prefetch_related(
                "reviews",
                "variants__color",
                "variants__size"
            )
            .annotate(
                average_rating=Avg("reviews__rating"),
                review_count=Count("reviews", distinct=True)
            )
            .order_by("id")
        )

        # ─────────────────────────────────────────────
        # Unfiltered price range (for price slider bounds)
        # Computed from the base queryset so the slider always
        # spans the full brand price range.
        # ─────────────────────────────────────────────
        price_bounds = base_products.aggregate(
            min_price=Min("price"),
            max_price=Max("price")
        )
        min_price_unfiltered = price_bounds["min_price"] or 0
        max_price_unfiltered = price_bounds["max_price"] or 0

        # ─────────────────────────────────────────────
        # Parse filter parameters from query string
        # ─────────────────────────────────────────────
        try:
            active_colors = list(map(int, request.GET.getlist("color")))
            active_sizes = list(map(int, request.GET.getlist("size")))
            active_vendors = list(map(int, request.GET.getlist("vendor")))
            active_ratings = list(map(int, request.GET.getlist("rating")))
            min_price = Decimal(request.GET.get("from")) if request.GET.get("from") else None
            max_price = Decimal(request.GET.get("to")) if request.GET.get("to") else None
        except ValueError:
            return Response({"detail": "Invalid filters"}, status=400)

        # ─────────────────────────────────────────────
        # Build a combined Q filter from all active parameters.
        # Filters are applied on top of base_products so all
        # narrowing happens within the brand's product set.
        # ─────────────────────────────────────────────
        filters = Q()
        if active_colors:
            filters &= Q(variants__color_id__in=active_colors)
        if active_sizes:
            filters &= Q(variants__size_id__in=active_sizes)
        if active_vendors:
            filters &= Q(vendor_id__in=active_vendors)
        if active_ratings:
            filters &= Q(average_rating__gte=min(active_ratings))
        if min_price is not None:
            filters &= Q(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filters &= Q(price__lte=max_price / exchange_rate)

        # Apply filters. Use .distinct() to prevent duplicate rows
        # caused by joining on variants (color/size).
        if filters:
            filtered_products = base_products.filter(filters).distinct()
        else:
            filtered_products = base_products

        # ─────────────────────────────────────────────
        # Filtered price range (reflects the narrowed-down set)
        # ─────────────────────────────────────────────
        filtered_bounds = filtered_products.aggregate(
            min_price=Min("price"),
            max_price=Max("price")
        )

        # ─────────────────────────────────────────────
        # Pagination
        # ─────────────────────────────────────────────
        paginator = PageNumberPagination()
        paginator.page_size = 12
        paged_products = paginator.paginate_queryset(filtered_products, request)

        # ─────────────────────────────────────────────
        # Serialize the paginated products with variant details.
        # Uses prefetched data so no extra DB queries are fired.
        # ─────────────────────────────────────────────
        product_data = ProductSerializer(
            paged_products, many=True, context={"request": request}
        ).data

        products_with_details = []
        for product in paged_products:
            variants = product.variants.all()
            # Build unique color set from prefetched variants
            color_set = {
                (v.color.name, v.color.code, v.id)
                for v in variants if v.color
            }

            products_with_details.append({
                "product": ProductSerializer(product, context={"request": request}).data,
                "average_rating": product.average_rating or 0,
                "review_count": product.review_count or 0,
                "variants": VariantSerializer(variants, many=True).data,
                "colors": [
                    {"name": c[0], "code": c[1], "id": c[2]}
                    for c in color_set
                ],
            })

        # ─────────────────────────────────────────────
        # Sidebar filter options — derived from the UNFILTERED
        # base queryset so the sidebar always shows every
        # available option for this brand.
        # ─────────────────────────────────────────────
        sizes_qs = Size.objects.filter(variants__product__in=base_products).distinct()
        colors_qs = Color.objects.filter(variants__product__in=base_products).distinct()
        vendors_qs = Vendor.objects.filter(product__in=base_products).distinct()

        # ─────────────────────────────────────────────
        # Build and return the response
        # ─────────────────────────────────────────────
        return Response({
            # Sidebar options (from unfiltered base)
            "colors": ColorSerializer(colors_qs, many=True).data,
            "sizes": SizeSerializer(sizes_qs, many=True).data,
            "vendors": VendorSerializer(vendors_qs, many=True).data,
            "brand": BrandSerializer(brand).data,

            # Product list (filtered + paginated)
            "products": product_data,
            "products_with_details": products_with_details,

            # Price ranges
            "min_price": round((filtered_bounds["min_price"] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((filtered_bounds["max_price"] or max_price_unfiltered) * exchange_rate, 2),
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),

            "currency": currency,
            "exchange_rate": exchange_rate,
            "default_max_price": round(Decimal('10000') * exchange_rate, 2),

            # Pagination links
            "next": paginator.get_next_link(),
            "previous": paginator.get_previous_link(),
            "total": filtered_products.count(),
        })

# from elasticsearch8 import Elasticsearch

import logging

# Configure logging
logger = logging.getLogger(__name__)
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank

from django.db.models.functions import Coalesce

class ProductSearchAPIView(APIView):
    """
    Full-text product search with filtering and pagination.

    SEARCH STRATEGY:
    1. Basic keyword matching (icontains on title/description) as a baseline.
    2. PostgreSQL full-text search layered on top for ranking (best match first).
       Falls back gracefully to keyword-only if full-text fails (e.g. SQLite dev).

    FILTERING STRATEGY (same pattern as BrandProductListView):
    1. The search results form the "base queryset" (unfiltered_qs).
    2. Sidebar filter options are derived from this base so they always
       reflect ALL available options from the search.
    3. User-selected filters narrow the display set without affecting the sidebar.
    """

    def get(self, request, format=None):
        query = (request.GET.get('q') or '').strip()

        # No query → return empty result immediately
        if not query:
            return Response({
                "products": [],
                "products_with_details": [],
                "total": 0,
                "min_price": 0,
                "max_price": 0,
                "min_price_unfiltered": 0,
                "max_price_unfiltered": 0,
                "colors": [],
                "sizes": [],
                "brands": [],
                "vendors": [],
                "categories": []
            })

        # ────────────────────────────────────────────────────────────────
        # Build base search queryset
        # ────────────────────────────────────────────────────────────────

        # Keyword search: split query into terms and match any term
        # against title or description (OR logic within terms).
        search_terms = query.split()
        q_filter = Q()
        for term in search_terms:
            q_filter |= (
                Q(title__icontains=term) |
                Q(description__icontains=term)
            )

        base_qs = (
            Product.objects
            .filter(status="published")
            .filter(q_filter)
            .annotate(
                average_rating=Coalesce(Avg('reviews__rating'), 0.0),
                review_count=Count('reviews')
            )
            .select_related("brand", "vendor", "sub_category")
            .prefetch_related("variants__color", "variants__size", "reviews")
        )

        # Try PostgreSQL full-text search for relevance ranking.
        # Wrapping in try/except so it degrades gracefully on SQLite or
        # if the DB doesn't have the required extensions.
        used_fulltext = False
        try:
            search_query = SearchQuery(query, config='english')
            base_qs = base_qs.annotate(
                search_vector=(
                    SearchVector('title', weight='A') +
                    SearchVector('description', weight='B') +
                    SearchVector('features', weight='C') +
                    SearchVector('specifications', weight='C')
                ),
                rank=SearchRank(
                    SearchVector('title', weight='A') +
                    SearchVector('description', weight='B') +
                    SearchVector('features', weight='C') +
                    SearchVector('specifications', weight='C'),
                    search_query
                )
            ).filter(search_vector=search_query)

            # Best match first, then newest
            base_qs = base_qs.order_by('-rank', '-date')
            used_fulltext = True

        except Exception as e:
            logger.warning(f"Full-text search failed, using keyword fallback: {e}")
            base_qs = base_qs.order_by('-date')

        # Fallback ordering if full-text didn't activate
        if not used_fulltext:
            base_qs = base_qs.order_by('-date')

        # ────────────────────────────────────────────────────────────────
        # Currency setup
        # ────────────────────────────────────────────────────────────────
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        # ────────────────────────────────────────────────────────────────
        # Unfiltered price range (for slider bounds)
        # Computed from the full search results before any user filters.
        # ────────────────────────────────────────────────────────────────
        unfiltered_price_range = base_qs.aggregate(
            min_price_unfiltered=Min('price'),
            max_price_unfiltered=Max('price')
        )
        min_price_unfiltered = unfiltered_price_range['min_price_unfiltered'] or 0
        max_price_unfiltered = unfiltered_price_range['max_price_unfiltered'] or 0

        # ────────────────────────────────────────────────────────────────
        # Parse filter parameters from query string
        # ────────────────────────────────────────────────────────────────
        try:
            active_colors = [int(i) for i in request.GET.getlist('color') if i.isdigit()]
            active_sizes = [int(i) for i in request.GET.getlist('size') if i.isdigit()]
            active_brands = [int(i) for i in request.GET.getlist('brand') if i.isdigit()]
            active_vendors = [int(i) for i in request.GET.getlist('vendor') if i.isdigit()]
            active_ratings = [int(i) for i in request.GET.getlist('rating') if i.isdigit()]
            min_price = Decimal(request.GET.get('from')) if request.GET.get('from') else None
            max_price = Decimal(request.GET.get('to')) if request.GET.get('to') else None
        except ValueError:
            return Response({"detail": "Invalid filter parameters"}, status=400)

        # ────────────────────────────────────────────────────────────────
        # Build combined filter and apply on top of search results.
        # base_qs is preserved as unfiltered_qs for sidebar options.
        # ────────────────────────────────────────────────────────────────
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
        if active_ratings:
            filters &= Q(average_rating__gte=min(active_ratings))

        # Preserve unfiltered search results for sidebar filter options.
        unfiltered_qs = base_qs

        # Apply filters. .distinct() prevents duplicate rows from variant joins.
        filtered_products = base_qs.filter(filters).distinct()

        # ────────────────────────────────────────────────────────────────
        # Filtered price range (reflects the narrowed-down set)
        # ────────────────────────────────────────────────────────────────
        price_range = filtered_products.aggregate(
            min_price=Min('price'),
            max_price=Max('price')
        )

        # ────────────────────────────────────────────────────────────────
        # Pagination (with page clamping for out-of-range requests)
        # ────────────────────────────────────────────────────────────────
        paginator = PageNumberPagination()
        paginator.page_size = 12

        try:
            total_items = filtered_products.count()
            total_pages = max(1, (total_items + paginator.page_size - 1) // paginator.page_size)
            requested_page = int(request.GET.get('page', '1'))

            if requested_page < 1 or requested_page > total_pages or total_items == 0:
                requested_page = 1

            # Temporarily make request.GET mutable to set the clamped page
            mutable = request.GET._mutable
            request.GET._mutable = True
            request.GET['page'] = str(requested_page)
            request.GET._mutable = mutable

            paged_products = paginator.paginate_queryset(filtered_products, request)

        except Exception as e:
            logger.error(f"Pagination error in ProductSearchAPIView: {e}")
            paged_products = []
            total_items = 0

        # ────────────────────────────────────────────────────────────────
        # Serialize products (flat list + enriched details with variants)
        # ────────────────────────────────────────────────────────────────
        serialized_products = ProductSerializer(
            paged_products,
            many=True,
            context={'request': request}
        ).data

        products_with_details = []
        for product in paged_products:
            variants = product.variants.all()
            variant_colors = variants.values('color__name', 'color__code', 'id').distinct()

            products_with_details.append({
                'product': ProductSerializer(product, context={'request': request}).data,
                'average_rating': product.average_rating or 0,
                'review_count': product.review_count or 0,
                'variants': VariantSerializer(variants, many=True).data,
                'colors': list(variant_colors),
            })

        # ────────────────────────────────────────────────────────────────
        # Sidebar filter options — from the UNFILTERED search results.
        # Always shows all available options so users can freely adjust.
        # ────────────────────────────────────────────────────────────────
        sizes = Size.objects.filter(variants__product__in=unfiltered_qs).distinct()
        colors_qs = Color.objects.filter(variants__product__in=unfiltered_qs).distinct()
        brands = Brand.objects.filter(product__in=unfiltered_qs).distinct()
        vendors = Vendor.objects.filter(product__in=unfiltered_qs).distinct()
        categories = Sub_Category.objects.filter(product__in=unfiltered_qs).distinct()

        # ────────────────────────────────────────────────────────────────
        # Build and return the response
        # ────────────────────────────────────────────────────────────────
        context = {
            # Sidebar options (from unfiltered search results)
            "colors": ColorSerializer(colors_qs, many=True).data,
            "sizes": SizeSerializer(sizes, many=True).data,
            "vendors": VendorSerializer(vendors, many=True).data,
            "brands": BrandSerializer(brands, many=True).data,
            "categories": SubCategorySerializer(categories, many=True).data,

            # Product list (filtered + paginated)
            "products": serialized_products,
            "products_with_details": products_with_details,

            # Price range after filters (for display)
            "min_price": round((price_range['min_price'] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((price_range['max_price'] or max_price_unfiltered) * exchange_rate, 2),

            # Price range before filters (for slider bounds)
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),
            "default_max_price": round(Decimal('10000') * exchange_rate, 2),

            "currency": currency,
            "next": paginator.get_next_link() if paged_products else None,
            "previous": paginator.get_previous_link() if paged_products else None,
            "total": total_items,
        }

        return Response(context)
         

class RecentlyViewedProducts(APIView):
    def get(self, request):
        products = get_recently_viewed_products(request, limit=10)
        if not products:
            data = []
        else:
            serializer = LightProductSerializer(  # ← Use a LIGHT serializer!
                products,
                many=True,
                context={'request': request}
            )
            data = serializer.data
        return Response(data)

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

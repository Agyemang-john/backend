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
    Ultra-optimized category product list view with:
    - Aggressive query optimization (select_related, prefetch_related, only())
    - Multi-level caching (category, filters, price ranges)
    - Minimized database hits
    - Optional result caching for common filter combinations
    """
    
    def get(self, request, slug):
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1: Fetch Category (with caching)
        # ═══════════════════════════════════════════════════════════════════════
        cache_key = f"category:{slug}"
        category = cache.get(cache_key)
        
        if not category:
            category = Sub_Category.objects.filter(slug=slug).first()
            if not category:
                return Response({"detail": "Category not found"}, status=404)
            cache.set(cache_key, category, 3600)  # Cache for 1 hour
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2: Extract and Parse Filters
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
        # STEP 3: Get Unfiltered Price Range (cached)
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
        # STEP 4: Build Optimized Base Queryset
        # ═══════════════════════════════════════════════════════════════════════
        base_queryset = Product.objects.filter(
            status="published",
            sub_category=category
        ).select_related(
            'brand', 'vendor', 'sub_category'
        ).prefetch_related(
            # Prefetch reviews - only need ratings
            Prefetch(
                'reviews',
                queryset=ProductReview.objects.filter(status=True).only('rating', 'product_id')
            ),
            # Prefetch variants with colors and sizes
            Prefetch(
                'variants',
                queryset=Variants.objects.select_related('color', 'size').only(
                    'id', 'product_id', 'color__id', 'color__name', 'color__code',
                    'size__id', 'size__name', 'quantity', 'price'
                )
            )
        ).only(
            # Only fetch fields we actually need
            'id', 'title', 'slug', 'sku', 'image', 'price', 'old_price',
            'brand__id', 'brand__title', 'brand__slug',
            'vendor__id', 'vendor__name',
            'sub_category__id', 'sub_category__title'
        )

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 5: Apply Filters with Single Query
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
        
        # Annotate ratings AFTER filtering but BEFORE distinct
        filtered_products = filtered_products.annotate(
            average_rating=Avg('reviews__rating'),
            review_count=Count('reviews', distinct=True)
        )
        
        if rating:
            filtered_products = filtered_products.filter(average_rating__gte=min(rating))
        
        # Apply distinct only if needed
        if needs_distinct:
            filtered_products = filtered_products.distinct()
        
        # Order for consistent pagination
        filtered_products = filtered_products.order_by('id')

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 6: Get Filtered Price Range (single query)
        # ═══════════════════════════════════════════════════════════════════════
        price_range = filtered_products.aggregate(
            max_price=Max('price'), 
            min_price=Min('price')
        )

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 7: Pagination (efficient counting and slicing)
        # ═══════════════════════════════════════════════════════════════════════
        PAGE_SIZE = 12
        
        try:
            total_items = filtered_products.count()
            total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
            
            # Clamp page number
            if page > total_pages and total_items > 0:
                page = 1
            elif page < 1:
                page = 1
            
            # Manual slicing instead of DRF paginator (faster)
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            
            # Execute the query and convert to list
            paged_products = list(filtered_products[start:end])
            
        except Exception as e:
            print(f"Pagination error: {e}")
            paged_products = []
            total_items = 0
            page = 1

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 8: Prepare Product Details (using prefetched data - NO extra queries)
        # ═══════════════════════════════════════════════════════════════════════
        products_with_details = []
        
        for product in paged_products:
            # These use prefetched data - NO database hit
            product_variants = list(product.variants.all())
            
            # Build unique colors dict
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
        # STEP 9: Get Filter Options (cached, separate from product queries)
        # ═══════════════════════════════════════════════════════════════════════
        filter_cache_key = f"filters:{slug}"
        filter_options = cache.get(filter_cache_key)
        
        if not filter_options:
            # Use values_list for minimal data transfer
            sizes = list(Size.objects.filter(
                variants__product__sub_category=category,
                variants__product__status="published"
            ).distinct().values('id', 'name'))
            
            colors = list(Color.objects.filter(
                variants__product__sub_category=category,
                variants__product__status="published"
            ).distinct().values('id', 'name', 'code'))
            
            brands = list(Brand.objects.filter(
                product__sub_category=category,
                product__status="published"
            ).distinct().values('id', 'title'))
            
            vendors = list(Vendor.objects.filter(
                product__sub_category=category,
                product__status="published"
            ).distinct().values('id', 'name'))
            
            filter_options = {
                "colors": colors,
                "sizes": sizes,
                "vendors": vendors,
                "brands": brands,
            }
            cache.set(filter_cache_key, filter_options, 3600)

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 10: Build Pagination URLs
        # ═══════════════════════════════════════════════════════════════════════
        def build_pagination_url(page_num):
            if page_num < 1 or page_num > total_pages or total_items == 0:
                return None
            params = request.GET.copy()
            params['page'] = page_num
            return f"/api/v1/product/category/{slug}/?{params.urlencode()}"

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 11: Build Response
        # ═══════════════════════════════════════════════════════════════════════
        context = {
            **filter_options,
            "category": SubCategorySerializer(category).data,
            "products": [p['product'] for p in products_with_details],
            "products_with_details": products_with_details,
            "min_price": round((price_range['min_price'] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((price_range['max_price'] or max_price_unfiltered) * exchange_rate, 2),
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),
            "default_max_price": round(Decimal('10000') * exchange_rate, 2),
            "currency": currency,
            "next": build_pagination_url(page + 1),
            "previous": build_pagination_url(page - 1) if page > 1 else None,
            "total": total_items,
            "current_page": page,
            "total_pages": total_pages,
        }
        
        return Response(context)

class BrandProductListView(APIView):
    def get(self, request, slug):
        currency = request.headers.get("X-Currency", "GHS")
        exchange_rate = Decimal(str(get_exchange_rates().get(currency, 1)))

        brand = Brand.objects.filter(slug=slug).first()
        if not brand:
            return Response({"detail": "Brand not found"}, status=404)

        # ─────────────────────────────────────────────
        # Base queryset (annotated ONCE)
        # ─────────────────────────────────────────────
        products = (
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
        # Unfiltered price range
        # ─────────────────────────────────────────────
        price_bounds = products.aggregate(
            min_price=Min("price"),
            max_price=Max("price")
        )

        min_price_unfiltered = price_bounds["min_price"] or 0
        max_price_unfiltered = price_bounds["max_price"] or 0

        # ─────────────────────────────────────────────
        # Filters
        # ─────────────────────────────────────────────
        try:
            colors = list(map(int, request.GET.getlist("color")))
            sizes = list(map(int, request.GET.getlist("size")))
            vendors = list(map(int, request.GET.getlist("vendor")))
            ratings = list(map(int, request.GET.getlist("rating")))
            min_price = Decimal(request.GET.get("from")) if request.GET.get("from") else None
            max_price = Decimal(request.GET.get("to")) if request.GET.get("to") else None
        except ValueError:
            return Response({"detail": "Invalid filters"}, status=400)

        filters = Q()

        if colors:
            filters &= Q(variants__color_id__in=colors)
        if sizes:
            filters &= Q(variants__size_id__in=sizes)
        if vendors:
            filters &= Q(vendor_id__in=vendors)
        if ratings:
            filters &= Q(average_rating__gte=min(ratings))
        if min_price:
            filters &= Q(price__gte=min_price / exchange_rate)
        if max_price:
            filters &= Q(price__lte=max_price / exchange_rate)

        if filters:
            products = products.filter(filters).distinct()

        # ─────────────────────────────────────────────
        # Filtered price range
        # ─────────────────────────────────────────────
        filtered_bounds = products.aggregate(
            min_price=Min("price"),
            max_price=Max("price")
        )

        # ─────────────────────────────────────────────
        # Pagination (DRF-native)
        # ─────────────────────────────────────────────
        paginator = PageNumberPagination()
        paginator.page_size = 12
        page = paginator.paginate_queryset(products, request)

        # ─────────────────────────────────────────────
        # Serialize once
        # ─────────────────────────────────────────────
        product_data = ProductSerializer(page, many=True, context={"request": request}).data

        # Build details using prefetched data (NO extra queries)
        products_with_details = []
        for product in page:
            variants = product.variants.all()
            colors = {
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
                    for c in colors
                ],
            })

        # ─────────────────────────────────────────────
        # Sidebar filters (derived from filtered products)
        # ─────────────────────────────────────────────
        sizes_qs = Size.objects.filter(variants__product__in=products).distinct()
        colors_qs = Color.objects.filter(variants__product__in=products).distinct()
        vendors_qs = Vendor.objects.filter(product__in=products).distinct()

        return Response({
            "colors": ColorSerializer(colors_qs, many=True).data,
            "sizes": SizeSerializer(sizes_qs, many=True).data,
            "vendors": VendorSerializer(vendors_qs, many=True).data,
            "brand": BrandSerializer(brand).data,

            "products": product_data,
            "products_with_details": products_with_details,

            "min_price": round((filtered_bounds["min_price"] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((filtered_bounds["max_price"] or max_price_unfiltered) * exchange_rate, 2),
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),

            "currency": currency,
            "exchange_rate": exchange_rate,
            "default_max_price": round(10000 * exchange_rate, 2),

            "next": paginator.get_next_link(),
            "previous": paginator.get_previous_link(),
            "total": products.count(),
        })

# from elasticsearch8 import Elasticsearch

import logging

# Configure logging
logger = logging.getLogger(__name__)
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank

from django.db.models.functions import Coalesce

class ProductSearchAPIView(APIView):
    def get(self, request, format=None):
        query = (request.GET.get('q') or '').strip()

        # If no search query → return empty result (strict behavior)
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

        # Base active/published products
        base_qs = Product.objects.filter(status="published")

        # Basic keyword search (title OR description) - split into terms for better matching
        search_terms = query.split()
        q_filter = Q()
        for term in search_terms:
            q_filter |= (
                Q(title__icontains=term) |
                Q(description__icontains=term)
                # You can add more fields later if needed:
                # | Q(brand__name__icontains=term)
                # | Q(sub_category__name__icontains=term)
            )

        base_qs = base_qs.filter(q_filter)

        # Common annotations & relations
        base_qs = (
            base_qs
            .annotate(
                average_rating=Coalesce(Avg('reviews__rating'), 0.0),
                review_count=Count('reviews')
            )
            .select_related("brand", "vendor", "sub_category")
            .prefetch_related("variants__color", "variants__size", "reviews")
        )

        # Try PostgreSQL full-text search (ranking) as enhancement
        used_fulltext = False
        if query:
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
                logger.warning(f"Full-text search failed: {str(e)}")
                # Fallback: normal icontains + date ordering
                base_qs = base_qs.order_by('-date')

        # If full-text didn't work → ensure we have sane default ordering
        if not used_fulltext:
            base_qs = base_qs.order_by('-date')

        # ────────────────────────────────────────────────────────────────
        # From here it's mostly your original logic with minor cleanups
        # ────────────────────────────────────────────────────────────────

        # Currency handling
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()  # Make sure this function exists!
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        # Unfiltered price range (for slider bounds)
        unfiltered_price_range = base_qs.aggregate(
            min_price_unfiltered=Min('price'),
            max_price_unfiltered=Max('price')
        )
        min_price_unfiltered = unfiltered_price_range['min_price_unfiltered'] or 0
        max_price_unfiltered = unfiltered_price_range['max_price_unfiltered'] or 0

        # Parse active filters
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

        # Build filter Q object
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

        # Apply filters + distinct (important when joining variants)
        filtered_products = base_qs.filter(filters).distinct()

        # Filtered price range for display
        price_range = filtered_products.aggregate(
            min_price=Min('price'),
            max_price=Max('price')
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
                
            # Update request.GET for pagination
            mutable = request.GET._mutable
            request.GET._mutable = True
            request.GET['page'] = str(requested_page)
            request.GET._mutable = mutable

            paged_products = paginator.paginate_queryset(filtered_products, request)
            
        except Exception:
            paged_products = []
            total_items = 0

        # Basic serialization
        serialized_products = ProductSerializer(
            paged_products, 
            many=True, 
            context={'request': request}
        ).data

        # Enriched product details (with variants & colors)
        products_with_details = []
        for product in paged_products:
            variants = product.variants.all()
            colors = variants.values('color__name', 'color__code', 'id').distinct()
            
            products_with_details.append({
                'product': ProductSerializer(product, context={'request': request}).data,
                'average_rating': product.average_rating or 0,
                'review_count': product.review_count or 0,
                'variants': VariantSerializer(variants, many=True).data,
                'colors': list(colors),
            })

        # Sidebar filter options (based on current filtered set)
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

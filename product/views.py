from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from .models import *
from order.models import *
from .serializers import *
from django.db.models import Avg, Count, Q, Max, Min, Prefetch
from address.serializers import AddressSerializer
from django.http import Http404
from django.core.cache import cache
from rest_framework.permissions import AllowAny
from rest_framework import status
from rest_framework.views import APIView
from decimal import Decimal
from order.service import *
from rest_framework.permissions import IsAuthenticated
from django.db.models import F
from rest_framework.pagination import PageNumberPagination

from .utils import (
    get_recently_viewed_products, update_recently_viewed, is_new_view,
    clear_recently_viewed, remove_recently_viewed, buffer_view_count,
    track_view, _get_redis,
)
from .shipping import can_product_ship_to_user

class AddProductReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        product_id = request.data.get('product')
        product = get_object_or_404(Product, id=product_id)

        if not self.user_has_purchased_product(request.user, product.id):
            return Response(
                {'detail': 'You must purchase and receive this product before reviewing it.'},
                status=status.HTTP_403_FORBIDDEN
            )

        if ProductReview.objects.filter(user=request.user, product=product).exists():
            return Response(
                {'detail': 'You have already reviewed this product.'},
                status=status.HTTP_409_CONFLICT
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
        "average_rating": product.avg_rating,
        "review_count": product.review_count,
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
    permission_classes = [AllowAny]

    def get(self, request, sku, slug):
        try:
            variant_id = request.GET.get('variantid')
            currency = request.headers.get('X-Currency', 'GHS')
            
            try:
                shared_data, product = get_cached_product_data(sku, slug, request)
            except Http404:
                return Response({"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND)
            
            # Tracking is intentionally omitted here.
            # All view tracking (recently-viewed + analytics) is done client-side via
            # POST /api/v1/product/mark-viewed/ from ProductDetail.tsx useEffect.
            # This prevents Next.js SSR renders, ISR rebuilds, and <Link> prefetch
            # requests from ghost-writing RecentlyViewedProduct rows for products
            # the user never actually visited.

            # Optimize variant queries
            variant = None
            if variant_id:
                variant = Variants.objects.filter(id=variant_id, product=product).first()
            if not variant:
                variant = Variants.objects.filter(product=product).first()
            
            stock_quantity = product.get_stock_quantity(variant)
            is_out_of_stock = stock_quantity <= 0

            can_ship, user_region = can_product_ship_to_user(request, product)

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

            # Wishlist check
            is_wishlisted = False
            wishlist_item_id = None
            if request.user.is_authenticated:
                wishlist_item = Wishlist.objects.filter(user=request.user, product=product).first()
                is_wishlisted = wishlist_item is not None
                wishlist_item_id = wishlist_item.id if wishlist_item else None

            # Fresh flash sale lookup — never cached, changes every minute
            # Variant-specific sale takes priority over product-level; product-level only
            # shows when no variant is selected or no variant-specific sale exists.
            from django.utils import timezone as tz
            from django.db.models import Case, When, IntegerField as _IntField
            now = tz.now()
            base_qs = FlashSale.objects.filter(
                product=product, is_active=True,
                start_time__lte=now, end_time__gte=now,
            )
            if variant:
                active_flash = (
                    base_qs
                    .filter(Q(variant=variant) | Q(variant__isnull=True))
                    .annotate(specificity=Case(
                        When(variant__isnull=False, then=0),
                        default=1,
                        output_field=_IntField(),
                    ))
                    .order_by('specificity')
                    .first()
                )
            else:
                active_flash = base_qs.filter(variant__isnull=True).first()
            flash_data = FlashSaleSerializer(active_flash, context={'request': request}).data if active_flash else None

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
                "user_region": user_region,
                "can_ship": can_ship,
                "is_wishlisted": is_wishlisted,
                "wishlist_item_id": wishlist_item_id,
                "active_flash_sale": flash_data,
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


class MarkProductViewedAPIView(APIView):
    """
    POST /api/v1/product/mark-viewed/
    Body: { "product_id": <int> }

    Called exclusively from the browser (ProductDetail.tsx useEffect) so only
    genuine user page visits are tracked — never SSR renders, ISR rebuilds, or
    Next.js <Link> prefetch requests.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        product_id = request.data.get('product_id')
        if not product_id:
            return Response({'error': 'product_id required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            product = Product.objects.only('id').get(id=product_id, status='published')
        except Product.DoesNotExist:
            return Response({'error': 'Product not found'}, status=status.HTTP_404_NOT_FOUND)

        update_recently_viewed(request, product.id)
        if request.user.is_authenticated:
            try:
                from product.tasks import sync_recently_viewed_db
                sync_recently_viewed_db.delay(request.user.pk, product.id)
            except Exception:
                pass
        track_view(request, product.id)

        return Response({'status': 'ok'}, status=status.HTTP_200_OK)


class ProductAuthStateAPIView(APIView):
    """
    GET /api/v1/product/<sku>/<slug>/auth-state/?variantid=<id>

    Returns cart status, wishlist status, vendor follow state, and shipping
    eligibility for the current user — logged-in OR guest.

    Called client-side from the browser (ProductDetail.tsx useEffect) so the
    browser automatically sends its session cookie, allowing Django to read
    the guest cart from request.session["guest_cart"] just like any other view.
    The main product endpoint stays cookie-free (ISR cacheable); this lightweight
    endpoint delivers personalised state within ~100ms of page mount.
    """
    permission_classes = [AllowAny]

    def get(self, request, sku, slug):
        product = get_object_or_404(Product, sku=sku, slug=slug, status='published')

        # Resolve the variant so the guest cart key matches what was stored.
        variant_id = request.GET.get('variantid')
        variant = None
        if variant_id:
            variant = Variants.objects.filter(id=variant_id, product=product).first()
        if not variant:
            variant = Variants.objects.filter(product=product).first()

        if request.user.is_authenticated:
            # ── Logged-in user: check DB cart ────────────────────────────────
            cart_data = {'is_in_cart': False, 'cart_quantity': 0, 'cart_item_id': None}
            try:
                cart = Cart.objects.get(user=request.user)
                cart_item = CartItem.objects.filter(
                    cart=cart, product=product, variant=variant
                ).only('id', 'quantity').first()
                if cart_item:
                    cart_data = {
                        'is_in_cart': True,
                        'cart_quantity': cart_item.quantity,
                        'cart_item_id': cart_item.id,
                    }
            except Cart.DoesNotExist:
                pass

            wishlist_item   = Wishlist.objects.filter(user=request.user, product=product).first()
            is_following    = product.vendor.followers.filter(id=request.user.id).exists()
            follower_count  = product.vendor.followers.count()
            can_ship, user_region = can_product_ship_to_user(request, product)

            has_reviewed = ProductReview.objects.filter(
                user=request.user, product=product
            ).exists()

            return Response({
                **cart_data,
                'is_wishlisted':    wishlist_item is not None,
                'wishlist_item_id': wishlist_item.id if wishlist_item else None,
                'is_following':     is_following,
                'follower_count':   follower_count,
                'can_ship':         can_ship,
                'user_region':      user_region,
                'has_reviewed':     has_reviewed,
            })

        else:
            # ── Guest: check session cart (browser sends sessionid cookie) ───
            item_key = f"{product.id}_{variant.id if variant else 'none'}"
            guest_cart = request.session.get("guest_cart", {})
            quantity = guest_cart.get(item_key, 0)
            can_ship, user_region = can_product_ship_to_user(request, product)

            return Response({
                'is_in_cart':       quantity > 0,
                'cart_quantity':    quantity,
                'cart_item_id':     None,
                'is_wishlisted':    False,
                'wishlist_item_id': None,
                'is_following':     False,
                'follower_count':   product.vendor.followers.count(),
                'can_ship':         can_ship,
                'user_region':      user_region,
            })


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
            Product.published.all()
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
    permission_classes = [AllowAny]

    def get(self, request, slug):
        # ── Category (cached 1 h) ──────────────────────────────────────────────
        cache_key = f"category:{slug}"
        category = cache.get(cache_key)
        if not category:
            category = Sub_Category.objects.filter(slug=slug).first()
            if not category:
                return Response({"detail": "Category not found"}, status=404)
            cache.set(cache_key, category, 3600)

        # Track subcategory page visit — deduped per visitor per 24 h, non-blocking.
        # Buffers into Redis; flush_subcategory_view_counts() drains to DB every 3 min.
        from product.utils import track_subcategory_view
        track_subcategory_view(request, category.id)

        # ── Currency ───────────────────────────────────────────────────────────
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        # ── Parse filters ──────────────────────────────────────────────────────
        try:
            active_colors  = [int(i) for i in request.GET.getlist('color')  if i.isdigit()]
            active_sizes   = [int(i) for i in request.GET.getlist('size')   if i.isdigit()]
            active_brands  = [int(i) for i in request.GET.getlist('brand')  if i.isdigit()]
            active_vendors = [int(i) for i in request.GET.getlist('vendor') if i.isdigit()]
            active_ratings = [int(i) for i in request.GET.getlist('rating') if i.isdigit()]
            min_price = Decimal(request.GET['from']) if request.GET.get('from') else None
            max_price = Decimal(request.GET['to'])   if request.GET.get('to')   else None
            page = max(1, int(request.GET.get('page', 1)))
        except (ValueError, TypeError):
            return Response({"detail": "Invalid filter parameters"}, status=400)

        sort = request.GET.get('sort', 'featured')
        _sort_map = {
            'trending':   '-trending_score',
            'price_asc':  'price',
            'price_desc': '-price',
            'rating':     '-avg_rating',
            'newest':     '-date',
            'oldest':     'date',
        }

        # ── Unfiltered price range for slider bounds (cached 1 h) ─────────────
        price_range_key = f"price_range:{slug}"
        unfiltered_price_range = cache.get(price_range_key)
        if not unfiltered_price_range:
            unfiltered_price_range = Product.published.filter(
                sub_category=category
            ).aggregate(min_price_unfiltered=Min('price'), max_price_unfiltered=Max('price'))
            cache.set(price_range_key, unfiltered_price_range, 3600)

        min_price_unfiltered = unfiltered_price_range.get('min_price_unfiltered') or Decimal('0')
        max_price_unfiltered = unfiltered_price_range.get('max_price_unfiltered') or Decimal('0')

        # ── Base queryset (no annotations — uses stored avg_rating/review_count) ─
        base_qs = Product.published.filter(
            sub_category=category
        ).select_related('brand', 'vendor', 'sub_category').prefetch_related(
            Prefetch(
                'variants',
                queryset=Variants.objects.select_related('color', 'size').only(
                    'id', 'product_id', 'price',
                    'color__id', 'color__name', 'color__code',
                    'size__id', 'size__name', 'quantity',
                )
            )
        )

        # ── Apply filters (subquery for variants — no JOIN/DISTINCT) ───────────
        filtered_qs = base_qs

        if active_colors:
            filtered_qs = filtered_qs.filter(
                id__in=Variants.objects.filter(color_id__in=active_colors).values('product_id')
            )
        if active_sizes:
            filtered_qs = filtered_qs.filter(
                id__in=Variants.objects.filter(size_id__in=active_sizes).values('product_id')
            )
        if active_brands:
            filtered_qs = filtered_qs.filter(brand_id__in=active_brands)
        if active_vendors:
            filtered_qs = filtered_qs.filter(vendor_id__in=active_vendors)
        if min_price is not None:
            filtered_qs = filtered_qs.filter(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filtered_qs = filtered_qs.filter(price__lte=max_price / exchange_rate)
        if active_ratings:
            filtered_qs = filtered_qs.filter(avg_rating__gte=min(active_ratings))

        # 'featured' and any unknown value → trending score (most engaging first)
        filtered_qs = filtered_qs.order_by(_sort_map.get(sort, '-trending_score'))

        # ── Filtered price range ───────────────────────────────────────────────
        price_range = filtered_qs.aggregate(min_price=Min('price'), max_price=Max('price'))

        # ── Pagination ─────────────────────────────────────────────────────────
        PAGE_SIZE = 12
        total_items = filtered_qs.count()
        total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages)
        paged_products = list(filtered_qs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE])

        # ── Serialize (lightweight — no reviews, no recursive VariantSerializer) ─
        products_with_details = []
        for product in paged_products:
            variants = list(product.variants.all())
            color_map = {}
            for v in variants:
                if v.color and v.color.id not in color_map:
                    color_map[v.color.id] = {
                        'id': v.id,
                        'color__name': v.color.name,
                        'color__code': v.color.code,
                    }
            products_with_details.append({
                'product': ProductListSerializer(product, context={'request': request}).data,
                'average_rating': product.avg_rating,
                'review_count': product.review_count,
                'colors': list(color_map.values()),
            })

        # ── Sidebar filter options (cached 1 h, from unfiltered base) ──────────
        filter_cache_key = f"filters:{slug}"
        filter_options = cache.get(filter_cache_key)
        if not filter_options:
            base_ids = base_qs.values('id')
            filter_options = {
                "colors":  list(Color.objects.filter(variants__product_id__in=base_ids).distinct().values('id', 'name', 'code')),
                "sizes":   list(Size.objects.filter(variants__product_id__in=base_ids).distinct().values('id', 'name')),
                "brands":  list(Brand.objects.filter(product__id__in=base_ids).distinct().values('id', 'title')),
                "vendors": list(Vendor.objects.filter(product__id__in=base_ids).distinct().values('id', 'name')),
            }
            cache.set(filter_cache_key, filter_options, 3600)

        def build_url(page_num):
            if page_num < 1 or page_num > total_pages or total_items == 0:
                return None
            params = request.GET.copy()
            params['page'] = page_num
            return f"/api/v1/product/category/{slug}/?{params.urlencode()}"

        return Response({
            **filter_options,
            "category": SubCategorySerializer(category).data,
            "products_with_details": products_with_details,
            "min_price": round((price_range['min_price'] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((price_range['max_price'] or max_price_unfiltered) * exchange_rate, 2),
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),
            "default_max_price": round(Decimal('10000') * exchange_rate, 2),
            "currency": currency,
            "next": build_url(page + 1),
            "previous": build_url(page - 1) if page > 1 else None,
            "total": total_items,
            "current_page": page,
            "total_pages": total_pages,
        })

class BrandProductListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug):
        # ── Currency ───────────────────────────────────────────────────────────
        currency = request.headers.get("X-Currency", "GHS")
        exchange_rate = Decimal(str(get_exchange_rates().get(currency, 1)))

        # ── Brand (cached 1 h) ─────────────────────────────────────────────────
        brand_cache_key = f"brand:{slug}"
        brand = cache.get(brand_cache_key)
        if not brand:
            brand = Brand.objects.filter(slug=slug).first()
            if not brand:
                return Response({"detail": "Brand not found"}, status=404)
            cache.set(brand_cache_key, brand, 3600)

        # Track brand page visit — deduped per visitor per 24 h, non-blocking.
        # Buffers into Redis; flush_brand_view_counts() drains to DB every 3 min.
        from product.utils import track_brand_view
        track_brand_view(request, brand.id)

        # ── Parse filters ──────────────────────────────────────────────────────
        try:
            active_colors  = [int(i) for i in request.GET.getlist("color")  if i.isdigit()]
            active_sizes   = [int(i) for i in request.GET.getlist("size")   if i.isdigit()]
            active_vendors = [int(i) for i in request.GET.getlist("vendor") if i.isdigit()]
            active_ratings = [int(i) for i in request.GET.getlist("rating") if i.isdigit()]
            min_price = Decimal(request.GET["from"]) if request.GET.get("from") else None
            max_price = Decimal(request.GET["to"])   if request.GET.get("to")   else None
            page = max(1, int(request.GET.get("page", 1)))
        except (ValueError, TypeError):
            return Response({"detail": "Invalid filters"}, status=400)

        sort = request.GET.get('sort', 'featured')
        _sort_map = {
            'trending':   '-trending_score',
            'price_asc':  'price',
            'price_desc': '-price',
            'rating':     '-avg_rating',
            'newest':     '-date',
            'oldest':     'date',
        }

        # ── Base queryset (uses stored avg_rating — no Avg JOIN) ───────────────
        base_qs = Product.published.filter(
            brand=brand
        ).select_related("vendor", "brand", "sub_category").prefetch_related(
            Prefetch(
                'variants',
                queryset=Variants.objects.select_related('color', 'size').only(
                    'id', 'product_id', 'price',
                    'color__id', 'color__name', 'color__code',
                    'size__id', 'size__name', 'quantity',
                )
            )
        )

        # ── Unfiltered price range for slider bounds (cached 1 h) ─────────────
        price_range_key = f"brand_price_range:{slug}"
        unfiltered_pr = cache.get(price_range_key)
        if not unfiltered_pr:
            unfiltered_pr = base_qs.aggregate(
                min_price_unfiltered=Min("price"),
                max_price_unfiltered=Max("price"),
            )
            cache.set(price_range_key, unfiltered_pr, 3600)

        min_price_unfiltered = unfiltered_pr["min_price_unfiltered"] or Decimal('0')
        max_price_unfiltered = unfiltered_pr["max_price_unfiltered"] or Decimal('0')

        # ── Apply filters (subquery for variants — no JOIN/DISTINCT) ───────────
        filtered_qs = base_qs

        if active_colors:
            filtered_qs = filtered_qs.filter(
                id__in=Variants.objects.filter(color_id__in=active_colors).values('product_id')
            )
        if active_sizes:
            filtered_qs = filtered_qs.filter(
                id__in=Variants.objects.filter(size_id__in=active_sizes).values('product_id')
            )
        if active_vendors:
            filtered_qs = filtered_qs.filter(vendor_id__in=active_vendors)
        if min_price is not None:
            filtered_qs = filtered_qs.filter(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filtered_qs = filtered_qs.filter(price__lte=max_price / exchange_rate)
        if active_ratings:
            filtered_qs = filtered_qs.filter(avg_rating__gte=min(active_ratings))

        # 'featured' and any unknown value → trending score
        filtered_qs = filtered_qs.order_by(_sort_map.get(sort, '-trending_score'))

        # ── Filtered price range ───────────────────────────────────────────────
        filtered_bounds = filtered_qs.aggregate(min_price=Min("price"), max_price=Max("price"))

        # ── Pagination ─────────────────────────────────────────────────────────
        PAGE_SIZE = 12
        total_items = filtered_qs.count()
        total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages)
        paged_products = list(filtered_qs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE])

        # ── Serialize (lightweight) ────────────────────────────────────────────
        products_with_details = []
        for product in paged_products:
            variants = list(product.variants.all())
            color_map = {}
            for v in variants:
                if v.color and v.color.id not in color_map:
                    color_map[v.color.id] = {
                        'id': v.id,
                        'color__name': v.color.name,
                        'color__code': v.color.code,
                    }
            products_with_details.append({
                "product": ProductListSerializer(product, context={"request": request}).data,
                "average_rating": product.avg_rating,
                "review_count": product.review_count,
                "colors": list(color_map.values()),
            })

        # ── Sidebar filter options (cached 1 h, from unfiltered base) ──────────
        brand_filter_cache_key = f"brand_filters:{slug}"
        brand_filter_options = cache.get(brand_filter_cache_key)
        if not brand_filter_options:
            base_ids = base_qs.values('id')
            brand_filter_options = {
                "colors":  list(Color.objects.filter(variants__product_id__in=base_ids).distinct().values('id', 'name', 'code')),
                "sizes":   list(Size.objects.filter(variants__product_id__in=base_ids).distinct().values('id', 'name')),
                "vendors": list(Vendor.objects.filter(product__id__in=base_ids).distinct().values('id', 'name')),
            }
            cache.set(brand_filter_cache_key, brand_filter_options, 3600)

        def build_url(page_num):
            if page_num < 1 or page_num > total_pages or total_items == 0:
                return None
            params = request.GET.copy()
            params['page'] = page_num
            return f"/api/v1/product/brand/{slug}/?{params.urlencode()}"

        return Response({
            **brand_filter_options,
            "brand": BrandSerializer(brand).data,
            "products_with_details": products_with_details,
            "min_price": round((filtered_bounds["min_price"] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((filtered_bounds["max_price"] or max_price_unfiltered) * exchange_rate, 2),
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),
            "default_max_price": round(Decimal('10000') * exchange_rate, 2),
            "currency": currency,
            "next": build_url(page + 1),
            "previous": build_url(page - 1) if page > 1 else None,
            "total": total_items,
            "total_pages": total_pages,
            "current_page": page,
        })

# from elasticsearch8 import Elasticsearch

import logging

# Configure logging
logger = logging.getLogger(__name__)
from django.contrib.postgres.search import SearchQuery, SearchRank

class ProductSearchAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, format=None):
        query = (request.GET.get('q') or '').strip()
        sort  = request.GET.get('sort', 'relevance')

        if not query:
            return Response({
                "products_with_details": [], "total": 0,
                "min_price": 0, "max_price": 0,
                "min_price_unfiltered": 0, "max_price_unfiltered": 0,
                "colors": [], "sizes": [], "brands": [], "vendors": [], "categories": [],
            })

        # ── Currency ───────────────────────────────────────────────────────────
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        # ── Parse filters ──────────────────────────────────────────────────────
        try:
            active_colors  = [int(i) for i in request.GET.getlist('color')  if i.isdigit()]
            active_sizes   = [int(i) for i in request.GET.getlist('size')   if i.isdigit()]
            active_brands  = [int(i) for i in request.GET.getlist('brand')  if i.isdigit()]
            active_vendors = [int(i) for i in request.GET.getlist('vendor') if i.isdigit()]
            active_ratings = [int(i) for i in request.GET.getlist('rating') if i.isdigit()]
            min_price = Decimal(request.GET['from']) if request.GET.get('from') else None
            max_price = Decimal(request.GET['to'])   if request.GET.get('to')   else None
            page = max(1, int(request.GET.get('page', 1)))
        except (ValueError, TypeError):
            return Response({"detail": "Invalid filter parameters"}, status=400)

        # ── Build base search queryset using stored search_vector (GIN index) ──
        search_query = SearchQuery(query, config='english')
        base_qs = (
            Product.published
            .filter(search_vector=search_query)
            .annotate(rank=SearchRank('search_vector', search_query))
            .select_related("brand", "vendor", "sub_category")
            .prefetch_related(
                Prefetch(
                    'variants',
                    queryset=Variants.objects.select_related('color', 'size').only(
                        'id', 'product_id', 'price',
                        'color__id', 'color__name', 'color__code',
                        'size__id', 'size__name', 'quantity',
                    )
                )
            )
        )

        # ── Unfiltered price range for slider bounds ────────────────────────────
        unfiltered_pr = base_qs.aggregate(
            min_price_unfiltered=Min('price'),
            max_price_unfiltered=Max('price'),
        )
        min_price_unfiltered = unfiltered_pr['min_price_unfiltered'] or Decimal('0')
        max_price_unfiltered = unfiltered_pr['max_price_unfiltered'] or Decimal('0')

        # ── Apply filters (subquery for variants — no JOIN/DISTINCT) ───────────
        filtered_qs = base_qs

        if active_colors:
            filtered_qs = filtered_qs.filter(
                id__in=Variants.objects.filter(color_id__in=active_colors).values('product_id')
            )
        if active_sizes:
            filtered_qs = filtered_qs.filter(
                id__in=Variants.objects.filter(size_id__in=active_sizes).values('product_id')
            )
        if active_brands:
            filtered_qs = filtered_qs.filter(brand_id__in=active_brands)
        if active_vendors:
            filtered_qs = filtered_qs.filter(vendor_id__in=active_vendors)
        if min_price is not None:
            filtered_qs = filtered_qs.filter(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filtered_qs = filtered_qs.filter(price__lte=max_price / exchange_rate)
        if active_ratings:
            filtered_qs = filtered_qs.filter(avg_rating__gte=min(active_ratings))

        # ── Sort ───────────────────────────────────────────────────────────────
        _sort_map = {
            'trending':   '-trending_score',
            'price_asc':  'price',
            'price_desc': '-price',
            'rating':     '-avg_rating',
            'newest':     '-date',
            'oldest':     'date',
        }
        if sort in _sort_map:
            filtered_qs = filtered_qs.order_by(_sort_map[sort])
        else:
            # 'relevance' (default): search rank first, trending score as tiebreaker
            filtered_qs = filtered_qs.order_by('-rank', '-trending_score')

        # ── Filtered price range ───────────────────────────────────────────────
        price_range = filtered_qs.aggregate(min_price=Min('price'), max_price=Max('price'))

        # ── Pagination ─────────────────────────────────────────────────────────
        PAGE_SIZE = 12
        total_items = filtered_qs.count()
        total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages)
        paged_products = list(filtered_qs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE])

        # ── Serialize (lightweight) ────────────────────────────────────────────
        products_with_details = []
        for product in paged_products:
            variants = list(product.variants.all())
            color_map = {}
            for v in variants:
                if v.color and v.color.id not in color_map:
                    color_map[v.color.id] = {
                        'id': v.id,
                        'color__name': v.color.name,
                        'color__code': v.color.code,
                    }
            products_with_details.append({
                'product': ProductListSerializer(product, context={'request': request}).data,
                'average_rating': product.avg_rating,
                'review_count': product.review_count,
                'colors': list(color_map.values()),
            })

        # ── Sidebar filter options from unfiltered base ────────────────────────
        base_ids = base_qs.values('id')
        colors_qs   = Color.objects.filter(variants__product_id__in=base_ids).distinct().values('id', 'name', 'code')
        sizes_qs    = Size.objects.filter(variants__product_id__in=base_ids).distinct().values('id', 'name')
        brands_qs   = Brand.objects.filter(product__id__in=base_ids).distinct().values('id', 'title', 'slug')
        vendors_qs  = Vendor.objects.filter(product__id__in=base_ids).distinct().values('id', 'name', 'slug')
        cats_qs     = Sub_Category.objects.filter(product__id__in=base_ids).distinct().values('id', 'title', 'slug')

        def build_url(page_num):
            if page_num < 1 or page_num > total_pages or total_items == 0:
                return None
            params = request.GET.copy()
            params['page'] = page_num
            return f"/api/v1/product/search/?{params.urlencode()}"

        return Response({
            "colors":     list(colors_qs),
            "sizes":      list(sizes_qs),
            "vendors":    list(vendors_qs),
            "brands":     list(brands_qs),
            "categories": list(cats_qs),
            "products_with_details": products_with_details,
            "min_price": round((price_range['min_price'] or min_price_unfiltered) * exchange_rate, 2),
            "max_price": round((price_range['max_price'] or max_price_unfiltered) * exchange_rate, 2),
            "min_price_unfiltered": round(min_price_unfiltered * exchange_rate, 2),
            "max_price_unfiltered": round(max_price_unfiltered * exchange_rate, 2),
            "default_max_price": round(Decimal('10000') * exchange_rate, 2),
            "currency": currency,
            "next": build_url(page + 1),
            "previous": build_url(page - 1) if page > 1 else None,
            "total": total_items,
            "total_pages": total_pages,
            "current_page": page,
        })
         

class RecentlyViewedProducts(APIView):
    def get(self, request):
        import math

        try:
            page      = max(1, int(request.GET.get('page', 1)))
            page_size = min(max(1, int(request.GET.get('page_size', 12))), 48)
        except (ValueError, TypeError):
            page, page_size = 1, 12

        if request.user.is_authenticated:
            from product.models import RecentlyViewedProduct
            rvp_qs = (
                RecentlyViewedProduct.objects
                .filter(user=request.user, product__status='published')
                .select_related('product')
                .order_by('-viewed_at')
            )
            total    = rvp_qs.count()
            offset   = (page - 1) * page_size
            products = [rv.product for rv in rvp_qs[offset:offset + page_size]]
        else:
            # Redis-backed for guests — load enough for pagination
            all_products = list(get_recently_viewed_products(request, limit=200))
            total    = len(all_products)
            offset   = (page - 1) * page_size
            products = all_products[offset:offset + page_size]

        total_pages = max(1, math.ceil(total / page_size))

        serializer = LightProductSerializer(products, many=True, context={'request': request})
        return Response({
            'results':     serializer.data,
            'count':       total,
            'page':        page,
            'page_size':   page_size,
            'total_pages': total_pages,
        })


class ClearRecentlyViewed(APIView):
    http_method_names = ['post']

    def post(self, request):
        clear_recently_viewed(request)          # Redis
        if request.user.is_authenticated:
            from product.models import RecentlyViewedProduct
            RecentlyViewedProduct.objects.filter(user=request.user).delete()
        return Response({"message": "Recently viewed cleared"}, status=status.HTTP_200_OK)


class RemoveRecentlyViewedItem(APIView):
    http_method_names = ['post']

    def post(self, request):
        product_id = request.data.get('product_id')
        if not product_id:
            return Response({"error": "product_id required"}, status=status.HTTP_400_BAD_REQUEST)
        pid = int(product_id)
        remove_recently_viewed(request, pid)    # Redis
        if request.user.is_authenticated:
            from product.models import RecentlyViewedProduct
            RecentlyViewedProduct.objects.filter(user=request.user, product_id=pid).delete()
        return Response({"message": "Item removed", "removed": str(product_id)})


class SyncRecentlyViewedView(APIView):
    """
    Called once after login to merge the guest's localStorage list into the
    backend. Accepts a list of product IDs (newest first, max 20).
    Redis rate-limit: one sync per user per 5 minutes.
    """
    permission_classes = [IsAuthenticated]
    http_method_names = ['post']

    def post(self, request):
        product_ids = request.data.get('product_ids', [])
        if not isinstance(product_ids, list):
            return Response({"error": "product_ids must be a list"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            product_ids = [int(pid) for pid in product_ids[:20]]
        except (ValueError, TypeError):
            return Response({"error": "Invalid product_ids"}, status=status.HTTP_400_BAD_REQUEST)

        # Rate-limit: one sync per user per 5 minutes
        conn = _get_redis()
        if conn:
            rate_key = f"rvp:sync:{request.user.pk}"
            if not conn.set(rate_key, 1, nx=True, ex=300):
                return Response({"message": "Already synced recently, try again in 5 minutes"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Process oldest-first so newest ends up at front of the Redis list
        for pid in reversed(product_ids):
            update_recently_viewed(request, pid)

        # Upsert DB records for cross-device access
        from product.tasks import sync_recently_viewed_db
        for pid in product_ids:
            sync_recently_viewed_db.delay(request.user.pk, pid)

        return Response({"synced": len(product_ids)})


class OccasionListAPIView(APIView):
    """
    Returns all active occasions (within date range) with their sections
    and 4 preview products per section. Cached 30 min.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        from django.db.models import Q as Qm
        today = timezone.now().date()
        cache_key = f'occasions_list_{request.headers.get("X-Currency","GHS")}'
        cached = cache.get(cache_key)
        if cached:
            return Response(cached)

        qs = Occasion.objects.filter(is_active=True).filter(
            Qm(start_date__isnull=True) | Qm(start_date__lte=today)
        ).filter(
            Qm(end_date__isnull=True) | Qm(end_date__gte=today)
        ).prefetch_related(
            Prefetch(
                'sections',
                queryset=OccasionSection.objects.select_related('collection').order_by('position')
            )
        )
        data = OccasionSerializer(qs, many=True, context={'request': request}).data
        cache.set(cache_key, data, 60 * 30)
        return Response(data)


class CollectionAPIView(APIView):
    """
    Returns a marketing collection (Back to School, Christmas, etc.) with its
    products and sidebar filter options.  Mirrors the CategoryProductListView
    response shape so the frontend can reuse the same product grid + sidebar.
    """
    permission_classes = [AllowAny]

    def get(self, request, slug):
        collection = get_object_or_404(Collection, slug=slug, is_active=True)

        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = Decimal(str(rates.get(currency, 1)))

        # Parse query params
        page = max(1, int(request.GET.get('page', 1) or 1))
        active_colors  = [int(v) for v in request.GET.getlist('color')  if v.isdigit()]
        active_sizes   = [int(v) for v in request.GET.getlist('size')   if v.isdigit()]
        active_brands  = [int(v) for v in request.GET.getlist('brand')  if v.isdigit()]
        active_vendors = [int(v) for v in request.GET.getlist('vendor') if v.isdigit()]
        active_ratings = [int(v) for v in request.GET.getlist('rating') if v.isdigit()]
        raw_from = request.GET.get('from')
        raw_to   = request.GET.get('to')
        min_price = Decimal(str(raw_from)) if raw_from else None
        max_price = Decimal(str(raw_to))   if raw_to   else None

        base_qs = collection.get_products_qs().select_related(
            'vendor', 'brand', 'sub_category'
        ).prefetch_related(
            Prefetch(
                'variants',
                queryset=Variants.objects.select_related('color', 'size').only(
                    'id', 'product_id', 'color__id', 'color__name', 'color__code',
                    'size__id', 'size__name', 'quantity', 'price',
                )
            )
        ).only(
            'id', 'title', 'slug', 'sku', 'image', 'price', 'old_price',
            'brand__id', 'brand__title', 'brand__slug',
            'vendor__id', 'vendor__name',
            'sub_category__id', 'sub_category__title',
        )

        filtered = base_qs

        if active_colors:
            filtered = filtered.filter(
                id__in=Variants.objects.filter(color_id__in=active_colors).values('product_id')
            )
        if active_sizes:
            filtered = filtered.filter(
                id__in=Variants.objects.filter(size_id__in=active_sizes).values('product_id')
            )
        if active_brands:
            filtered = filtered.filter(brand_id__in=active_brands)
        if active_vendors:
            filtered = filtered.filter(vendor_id__in=active_vendors)
        if min_price is not None:
            filtered = filtered.filter(price__gte=min_price / exchange_rate)
        if max_price is not None:
            filtered = filtered.filter(price__lte=max_price / exchange_rate)
        if active_ratings:
            filtered = filtered.filter(avg_rating__gte=min(active_ratings))

        sort = request.GET.get('sort', 'featured')
        _sort_map = {'price_asc': 'price', 'price_desc': '-price', 'rating': '-avg_rating', 'newest': '-date'}
        filtered = filtered.order_by(_sort_map.get(sort, '-date'))

        PAGE_SIZE = 12
        total_items = filtered.count()
        total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        paged = list(filtered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE])

        products_with_details = []
        for product in paged:
            variants = list(product.variants.all())
            color_map = {}
            for v in variants:
                if v.color and v.color.id not in color_map:
                    color_map[v.color.id] = {
                        'color__name': v.color.name,
                        'color__code': v.color.code,
                        'id': v.id,
                    }
            products_with_details.append({
                'product': ProductListSerializer(product, context={'request': request}).data,
                'average_rating': product.avg_rating,
                'review_count': product.review_count,
                'colors': list(color_map.values()),
            })

        # Sidebar filter options from the unfiltered base
        filter_cache_key = f"collection_filters:{slug}"
        filter_options = cache.get(filter_cache_key)
        if not filter_options:
            sizes   = list(Size.objects.filter(variants__product__in=base_qs).distinct().values('id', 'name'))
            colors  = list(Color.objects.filter(variants__product__in=base_qs).distinct().values('id', 'name', 'code'))
            brands  = list(Brand.objects.filter(product__in=base_qs).distinct().values('id', 'title', 'slug'))
            vendors = list(Vendor.objects.filter(product__in=base_qs).distinct().values('id', 'name', 'slug'))
            filter_options = {'sizes': sizes, 'colors': colors, 'brands': brands, 'vendors': vendors}
            cache.set(filter_cache_key, filter_options, timeout=3600)

        price_agg = base_qs.aggregate(min_price=Min('price'), max_price=Max('price'))

        next_url = request.build_absolute_uri(
            f"/api/v1/product/collection/{slug}/?page={page + 1}"
        ) if page < total_pages else None
        prev_url = request.build_absolute_uri(
            f"/api/v1/product/collection/{slug}/?page={page - 1}"
        ) if page > 1 else None

        return Response({
            'collection': CollectionSerializer(collection, context={'request': request}).data,
            'products_with_details': products_with_details,
            'next': next_url,
            'previous': prev_url,
            'total': total_items,
            'min_price_unfiltered': float(price_agg['min_price'] or 0) * float(exchange_rate),
            'max_price_unfiltered': float(price_agg['max_price'] or 0) * float(exchange_rate),
            'default_max_price':    float(price_agg['max_price'] or 10000) * float(exchange_rate),
            **filter_options,
            'currency': currency,
        }, status=status.HTTP_200_OK)


class FlashSaleListAPIView(APIView):
    """
    Returns all currently live flash sales (is_active=True, within time window).
    Cached for 30 seconds so every page load doesn't hit the DB,
    but the countdown timer still feels real-time.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        from django.utils import timezone as tz
        cache_key = "flash_sales_live"
        data = cache.get(cache_key)

        if data is None:
            now = tz.now()
            sales = (
                FlashSale.objects
                .filter(is_active=True, start_time__lte=now, end_time__gte=now)
                .select_related('product', 'variant', 'created_by')
                .order_by('end_time')
            )
            serializer = FlashSaleSerializer(sales, many=True, context={'request': request})
            data = serializer.data
            cache.set(cache_key, data, timeout=30)

        return Response(data, status=status.HTTP_200_OK)

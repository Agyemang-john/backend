from django.shortcuts import render, get_object_or_404
from rest_framework.response import Response
from .models import *
from order.models import *
from .serializers import *
from django.db.models import Avg, Count
from userauths.authentication import CustomJWTAuthentication
from address.serializers import AddressSerializer

from django.core.cache import cache
from rest_framework.permissions import AllowAny
from product.service import get_recommended_products, get_cart_based_recommendations
from rest_framework import status
from rest_framework.views import APIView

from order.service import *
from .service import get_fbt_recommendations, get_cart_product_ids
from .shipping import can_product_ship_to_user
from copy import deepcopy

class ProductsAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Get all published products, optionally ordered
        products = Product.published.all().order_by('-trending_score')  # remove [:10]
        serialized = ProductSerializer(products, many=True, context={'request': request}).data

        # Return as a list, not inside an object
        return Response(serialized)
    

class AjaxColorAPIView(APIView):
    def post(self, request, *args, **kwargs):
        size_id = request.data.get('size')
        product_id = request.data.get('productid')
        
        # Fetch the product by ID
        product = get_object_or_404(Product, id=product_id)
        
        # Fetch variants based on product ID and size ID
        colors = Variants.objects.filter(product_id=product_id, size_id=size_id)

        # Serialize the product and variants data
        product_data = ProductSerializer(product, context={'request': request}).data
        colors_data = VariantSerializer(colors, many=True, context={'request': request}).data
        
        # Prepare the response data
        response_data = {
            'product': product_data,
            'colors': colors_data
        }
        
        # Return the JSON response
        return Response(response_data, status=status.HTTP_200_OK)



def get_cached_product_data(sku, slug, request, currency):
    cache_key = f"product_detail_cache:{sku}:{slug}:{currency}"
    cached_data = cache.get(cache_key)

    if cached_data:
        return deepcopy(cached_data), Product.objects.get(sku=sku, slug=slug)
    
    product = get_object_or_404(
        Product.objects.annotate(
            average_rating=Avg('reviews__rating'),
            review_count=Count('reviews')
        ),
        slug=slug,
        status='published',
        sku=sku
    )

    p_images = ProductImageSerializer(product.p_images.all(), many=True, context={'request': request}).data
    related_products = Product.objects.filter(sub_category=product.sub_category, status="published").exclude(id=product.id)[:10]
    vendor_products = Product.objects.filter(vendor=product.vendor, status="published").exclude(id=product.id)[:10]
    reviews = ProductReview.objects.filter(product=product, status=True).order_by("-date")
    delivery_options = ProductDeliveryOption.objects.filter(product=product)

    shared_data = {
        "product": ProductSerializer(product, context={'request': request}).data,
        "p_images": p_images,
        "related_products": ProductSerializer(related_products, many=True, context={'request': request}).data,
        "vendor_products": ProductSerializer(vendor_products, many=True, context={'request': request}).data,
        "reviews": ProductReviewSerializer(reviews, many=True, context={'request': request}).data,
        'average_rating': product.average_rating or 0,
        'review_count': product.review_count or 0,
        'delivery_options': ProductDeliveryOptionSerializer(delivery_options, many=True).data
    }

    cache.set(cache_key, shared_data, timeout=600)
    return deepcopy(shared_data), product


def convert_currency(product_data, currency):
    product_data['product']['currency'] = currency
    product_data['product']['price'] = round(product_data['product']['price'], 2)
    product_data['product']['old_price'] = round(product_data['product']['old_price'], 2)

    for list_name in ['related_products', 'vendor_products']:
        for p in product_data[list_name]:
            p['currency'] = currency
            p['price'] = round(p['price'], 2)
            p['old_price'] = round(p['old_price'], 2)

    return product_data


class ProductDetailAPIView(APIView):
    authentication_classes = [CustomJWTAuthentication]
    
    def get(self, request, sku, slug):
        try:
            variant_id = request.GET.get('variantid')
            currency = request.headers.get('X-Currency', 'GHS')
            rates = get_exchange_rates()
            exchange_rate = rates.get(currency, 1)

            # 🔁 Get cached or DB data
            shared_data, product = get_cached_product_data(sku, slug, request, currency)
            shared_data = convert_currency(shared_data, currency)

            # 🔄 Fresh: variant, stock, shipping, cart
            variant = Variants.objects.get(id=variant_id) if variant_id else Variants.objects.filter(product=product).first()
            stock_quantity = variant.quantity if variant else product.total_quantity
            is_out_of_stock = stock_quantity < 1

            can_ship, user_region = can_product_ship_to_user(request, product)

            variant_data = {}
            if product.variant != "None" and variant:
                variant_data = {
                    'variant': VariantSerializer(variant, context={'request': request}).data,
                    'variant_images': VariantImageSerializer(VariantImage.objects.filter(variant=variant), many=True, context={'request': request}).data,
                    'colors': VariantSerializer(Variants.objects.filter(product=product, size=variant.size), many=True, context={'request': request}).data,
                    'sizes': VariantSerializer(Variants.objects.raw(
                        'SELECT * FROM product_variants WHERE product_id=%s GROUP BY size_id', [product.id]
                    ), many=True, context={'request': request}).data,
                }

            is_following = (
                request.user.is_authenticated and
                product.vendor.followers.filter(id=request.user.id).exists()
            )
            follower_count = product.vendor.followers.count()

            address = None
            if request.user.is_authenticated:
                address = Address.objects.filter(user=request.user, status=True).first()

            cart_data = {
                'is_in_cart': False,
                'cart_quantity': 0,
                'cart_item_id': None
            }

            if request.auth:
                try:
                    cart = Cart.objects.get_for_request(request)
                    cart_item = CartItem.objects.filter(cart=cart, product=product, variant=variant).first()
                    cart_data.update({
                        'is_in_cart': bool(cart_item),
                        'cart_quantity': cart_item.quantity if cart_item else 0,
                        'cart_item_id': getattr(cart_item, 'id', None)
                    })
                except Exception as e:
                    logger.warning(f"Authenticated cart error: {e}")
            else:
                guest_cart = request.headers.get('X-Guest-Cart')
                try:
                    guest_cart = json.loads(guest_cart) if guest_cart else []
                    for item in guest_cart:
                        if str(item.get('p')) == str(product.id) and (
                            str(item.get('v')) == str(variant.id) if variant else True
                        ):
                            cart_data.update({
                                'is_in_cart': True,
                                'cart_quantity': int(item.get('q', 0)),
                            })
                            break
                except Exception:
                    pass

            if cart_data["cart_quantity"] >= stock_quantity and stock_quantity != 0:
                is_out_of_stock = True

            return Response({
                **shared_data,
                "address": AddressSerializer(address).data if address else None,
                "variant_data": variant_data,
                "is_out_of_stock": is_out_of_stock,
                "available_stock": stock_quantity,
                "is_in_cart": cart_data["is_in_cart"],
                "cart_quantity": cart_data["cart_quantity"],
                "cart_item_id": cart_data["cart_item_id"],
                'is_following': is_following,
                'follower_count': follower_count,
                "user_region": user_region,
                "can_ship": can_ship
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Product detail error: {str(e)}", exc_info=True)
            return Response(
                {"error": "Failed to load product data", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db.models import Avg, Count, Q, Min, Max


class SearchSuggestionsAPIView(APIView):
    def get(self, request, *args, **kwargs):
        query = request.GET.get("q", "").strip()

        if query:
            suggestions_qs = (
                Product.objects
                .filter(title__icontains=query, status="published")
                .select_related("sub_category")
                .order_by("title")  # Optional: helps stable ordering
            )

            suggestions = []
            for product in suggestions_qs[:10]:  # slicing here avoids `.distinct()` errors
                suggestions.append({
                    "title": product.title,
                    "price": product.price,
                    "thumbnail": request.build_absolute_uri(product.image.url),
                    "category": product.sub_category.title if product.sub_category else "Uncategorized",
                })

            return Response(suggestions, status=status.HTTP_200_OK)

        return Response([], status=status.HTTP_200_OK)

class CategoryProductListView(APIView):
    def get(self, request, slug):
        # Fetch the category by slug
        category = Sub_Category.objects.filter(slug=slug).first()
        if not category:
            return Response({"detail": "Category not found"}, status=404)
        
        # Extract currency and exchange rate
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = rates.get(currency, 1)

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
            min_price = float(request.GET.get('from')) if request.GET.get('from') else None
            max_price = float(request.GET.get('to')) if request.GET.get('to') else None
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
        exchange_rate = rates.get(currency, 1)
        
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
            min_price = float(request.GET.get('from')) if request.GET.get('from') else None
            max_price = float(request.GET.get('to')) if request.GET.get('to') else None
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

from elasticsearch8 import Elasticsearch

import logging

# Configure logging
logger = logging.getLogger(__name__)

class ProductSearchAPIView(APIView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.es = Elasticsearch(hosts=["http://localhost:9200"], request_timeout=30)

    def get(self, request, format=None):
        query = request.GET.get('q', '').strip()

        # Initialize base queryset and aggregations
        base_queryset = Product.objects.filter(title__icontains=query, status="published").annotate(
            average_rating=Avg('reviews__rating'),
            review_count=Count('reviews')
        ).order_by('id')

        # Currency setup
        currency = request.headers.get('X-Currency', 'GHS')
        rates = get_exchange_rates()
        exchange_rate = rates.get(currency, 1)

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
            min_price = float(request.GET.get('from')) if request.GET.get('from') else None
            max_price = float(request.GET.get('to')) if request.GET.get('to') else None
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

        # === Elasticsearch Integration ===
        product_ids = []
        if query:
            try:
                body = {
                    "query": {
                        "multi_match": {
                            "query": query,
                            "fields": ["title^2", "description"],
                            "fuzziness": "AUTO"
                        }
                    },
                    "size": 1000,
                    "_source": ["id"]
                }

                response = self.es.search(index="products", body=body)
                product_ids = [hit["_source"]["id"] for hit in response["hits"]["hits"]]

                filters &= Q(id__in=product_ids)
            except Exception as e:
                logger.error(f"Elasticsearch error: {str(e)}")

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



class CartDataView(APIView):
    authentication_classes = [CustomJWTAuthentication]  # Ensure auth works

    def get(self, request, sku, slug):
        product = get_object_or_404(Product, slug=slug, sku=sku)
        variant_id = request.GET.get('variantid')

        is_following = (
            request.user.is_authenticated and
            product.vendor.followers.filter(id=request.user.id).exists()
        )
        
        try:
            variant = Variants.objects.get(id=variant_id) if variant_id else Variants.objects.filter(product=product).first()
        except Variants.DoesNotExist:
            variant = None

        cart = Cart.objects.get_for_request(request)
        cart_item = CartItem.objects.filter(cart=cart, product=product, variant=variant).first()

        return Response({
            'is_in_cart': bool(cart_item),
            'is_following': is_following,
            'cart_quantity': cart_item.quantity if cart_item else 0,
            'cart_item_id': cart_item.id if cart_item else None,
        }, status=status.HTTP_200_OK)
         

class RecentlyViewedProducts(APIView):
    def get(self, request):
        ids = request.GET.get("ids", "")
        id_list = [int(i) for i in ids.split(",") if i.isdigit()]
        products = Product.objects.filter(id__in=id_list)
        
        id_order = {id_: idx for idx, id_ in enumerate(id_list)}
        sorted_products = sorted(products, key=lambda p: id_order.get(p.id, 0))
        
        return Response(ProductSerializer(sorted_products, many=True, context={'request': request}).data)


class CartRecommendationsAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        user = request.auth
        guest_cart_raw = request.headers.get('X-Guest-Cart')
        

        # Get current cart product IDs
        if user:
            cart_product_ids = CartItem.objects.filter(cart__user=request.user).values_list('product_id', flat=True)
        elif guest_cart_raw:
            try:
                guest_items = json.loads(guest_cart_raw)
                cart_product_ids = [item['p'] for item in guest_items]
            except json.JSONDecodeError:
                cart_product_ids = []
        else:
            cart_product_ids = []

        # 1. Frequently bought together
        bought_together_set = set()
        for pid in cart_product_ids:
            related_products = get_cart_based_recommendations(pid)
            bought_together_set.update(related_products.values_list('id', flat=True))

        bought_together = Product.objects.filter(id__in=bought_together_set).exclude(id__in=cart_product_ids)[:10]
        bought_together_serialized = ProductSerializer(bought_together, many=True, context={'request': request}).data

        # 2. Personalized
        personalized = get_recommended_products(request)
        personalized_serialized = ProductSerializer(personalized, many=True, context={'request': request}).data

        return Response({
            "frequently_bought_together": bought_together_serialized,
            "recommended_for_you": personalized_serialized,
        })
    

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

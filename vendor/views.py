# vendor/views.py

from django.core.cache import cache
from datetime import date
from django.shortcuts import get_object_or_404
from core.models import *
from .models import *
from userauths.models import *
from order.models import *
from product.models import Product, Variants

from rest_framework import status, generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.exceptions import NotFound, APIException, PermissionDenied
from rest_framework.throttling import UserRateThrottle
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend

from django.db.models import Avg, Count, Sum, F
from django.db.models.functions import TruncWeek, TruncMonth, TruncDate
from django.utils import timezone
from datetime import timedelta
from django.http import Http404
from django.core.exceptions import ObjectDoesNotExist

from .models import About, Vendor, OpeningHour, VendorPaymentMethod
from .serializers import *
from .analytics_serializers import (
    SalesSummarySerializer, SalesTrendSerializer, TopProductSerializer,
    OrderStatusSerializer, EngagementSerializer, DeliveryPerformanceSerializer,
)
from .signup_serializers import VendorSignupSerializer
from .hour_serializers import OpeningHourSerializer
from .about_serializers import AboutSerializer
from .payment_serializers import VendorPaymentMethodSerializer, PayoutSerializer
from .product_serializers import ProductSerializer, ProductReviewSerializer
from .order_serializers import VendorOrderSerializer, OrderSerializer

from core.serializers import VendorSerializer as VendorDetail, ProductReviewSerializer as ReviewDetail
from vendor.permissions import IsVerifiedVendor
from order.models import OrderProduct, Order
from product.models import Wishlist, SavedProduct, ProductReview
from payments.models import Payout

# ── Subscription permission classes ───────────────────────────────────────────
from payments.subscription_permissions import (
    SubscriptionGateMixin,
    RequireBasicPlan,
    RequireProPlan,
    RequireEnterprisePlan,
    require_feature,
)

from dateutil.parser import parse
import requests, logging, difflib, re
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Analytics — require Basic plan minimum
# Vendors on Free get a 403 with upgrade_url in the body.
# ─────────────────────────────────────────────────────────────────────────────

class SalesSummaryView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor, RequireBasicPlan]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        ops = OrderProduct.objects.filter(product__vendor=vendor)
        active_ops = ops.exclude(status='canceled')

        delivered_ops = active_ops.filter(status='delivered', delivered_date__isnull=False)
        total_delivered = delivered_ops.count()
        on_time_count = delivered_ops.filter(
            delivered_date__lte=F('date_created') + F('selected_delivery_option__max_days') * timedelta(days=1)
        ).count()
        on_time_delivery_rate = (on_time_count / total_delivered * 100) if total_delivered > 0 else 0

        refunded_ops = ops.filter(refund_reason__isnull=False).count()
        refund_rate = (refunded_ops / ops.count() * 100) if ops.count() > 0 else 0

        data = {
            'total_revenue': active_ops.aggregate(Sum('amount'))['amount__sum'] or 0,
            'total_orders': active_ops.values('order').distinct().count(),
            'total_units_sold': active_ops.aggregate(Sum('quantity'))['quantity__sum'] or 0,
            'avg_order_value': active_ops.aggregate(
                avg=Avg(F('amount') / F('order__id'), output_field=models.FloatField())
            )['avg'] or 0,
            'cancellation_rate': (ops.filter(status='canceled').count() / ops.count() * 100) if ops.count() > 0 else 0,
            'refund_rate': refund_rate,
            'on_time_delivery_rate': on_time_delivery_rate,
            'avg_rating': ProductReview.objects.filter(product__vendor=vendor).aggregate(Avg('rating'))['rating__avg'] or 0,
            'total_views': vendor.views,
            'wishlist_count': Wishlist.objects.filter(product__vendor=vendor).count(),
        }
        return Response(SalesSummarySerializer(data).data)


class SalesTrendView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor, RequireBasicPlan]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        period = request.query_params.get('period', 'day')
        start_date_str = request.query_params.get('start_date')
        end_date_str   = request.query_params.get('end_date')

        if start_date_str and '?refresh=' in start_date_str:
            start_date_str = start_date_str.split('?refresh=')[0]
        if end_date_str and '?refresh=' in end_date_str:
            end_date_str = end_date_str.split('?refresh=')[0]

        try:
            start_date = parse(start_date_str) if start_date_str else timezone.now() - timedelta(days=30)
            end_date   = parse(end_date_str)   if end_date_str   else timezone.now()
        except (ValueError, TypeError):
            return Response({"error": "Invalid date format"}, status=status.HTTP_400_BAD_REQUEST)

        if not timezone.is_aware(start_date):
            start_date = timezone.make_aware(start_date)
        if not timezone.is_aware(end_date):
            end_date = timezone.make_aware(end_date)

        if start_date > end_date:
            return Response({"error": "start_date cannot be later than end_date"}, status=status.HTTP_400_BAD_REQUEST)

        ops = OrderProduct.objects.filter(
            product__vendor=vendor,
            order__date_created__gte=start_date,
            order__date_created__lte=end_date,
        ).exclude(status='canceled')

        trunc_fn = {'week': TruncWeek, 'month': TruncMonth}.get(period, TruncDate)
        trend = (
            ops.annotate(date=trunc_fn('order__date_created'))
            .values('date')
            .annotate(revenue=Sum('amount'), orders=Count('order', distinct=True))
            .order_by('date')
        )

        trend_data = [
            {
                'date': item['date'].date() if hasattr(item['date'], 'date') else item['date'],
                'revenue': float(item['revenue'] or 0),
                'orders': item['orders'] or 0,
            }
            for item in trend
        ]
        return Response(SalesTrendSerializer(trend_data, many=True).data)


class TopProductsView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor, RequireBasicPlan]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        top_products = (
            OrderProduct.objects.filter(product__vendor=vendor)
            .exclude(status='canceled')
            .values('product__id', 'product__title')
            .annotate(revenue=Sum('amount'), units_sold=Sum('quantity'))
            .order_by('-revenue')[:10]
        )
        return Response(TopProductSerializer(top_products, many=True).data)


class OrderStatusView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor, RequireBasicPlan]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        statuses = (
            OrderProduct.objects.filter(product__vendor=vendor)
            .values('status')
            .annotate(count=Count('id'))
        )
        return Response(OrderStatusSerializer(statuses, many=True).data)


class EngagementView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor, RequireBasicPlan]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        data = {
            'total_views':    vendor.views,
            'wishlist_count': Wishlist.objects.filter(product__vendor=vendor).count(),
            'saved_count':    SavedProduct.objects.filter(product__vendor=vendor).count(),
            'review_count':   ProductReview.objects.filter(product__vendor=vendor).count(),
            'avg_rating':     ProductReview.objects.filter(product__vendor=vendor).aggregate(Avg('rating'))['rating__avg'] or 0,
        }
        return Response(EngagementSerializer(data).data)


class DeliveryPerformanceView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor, RequireBasicPlan]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        delivered_ops = OrderProduct.objects.filter(
            product__vendor=vendor, status='delivered', delivered_date__isnull=False
        ).exclude(status='canceled')
        total_delivered = delivered_ops.count()
        on_time_count   = delivered_ops.filter(
            delivered_date__lte=F('date_created') + F('selected_delivery_option__max_days') * timedelta(days=1)
        ).count()
        overdue_count   = delivered_ops.filter(
            delivered_date__gt=F('date_created') + F('selected_delivery_option__max_days') * timedelta(days=1)
        ).count()

        data = {
            'on_time_delivery_rate': (on_time_count / total_delivered * 100) if total_delivered > 0 else 0,
            'total_delivered':       total_delivered,
            'overdue_deliveries':    overdue_count,
        }
        return Response(DeliveryPerformanceSerializer(data).data)


# ─────────────────────────────────────────────────────────────────────────────
# Product CRUD — gated by product count + image limits
# ─────────────────────────────────────────────────────────────────────────────

class IsVendorOwner(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return hasattr(request.user, 'vendor') and obj.vendor == request.user.vendor


class ProductListCreateView(SubscriptionGateMixin, generics.ListCreateAPIView):
    serializer_class   = ProductSerializer
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    check_product_limit = True
    check_image_limit   = True

    def get_queryset(self):
        vendor = self.request.user.vendor_user
        if not vendor:
            raise serializers.ValidationError("Vendor profile is required.")
        return Product.objects.filter(vendor=vendor)

    # ✅ No perform_create — mixin handles everything
    def get_perform_create_kwargs(self) -> dict:
        vendor = self.request.user.vendor_user
        if not vendor:
            raise serializers.ValidationError("Vendor profile is required.")
        return {'vendor': vendor}


class ProductCreateView(SubscriptionGateMixin, generics.CreateAPIView):
    queryset           = Product.objects.all()
    serializer_class   = ProductSerializer
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    check_product_limit = True
    check_image_limit   = True

    def perform_create(self, serializer):
        vendor = self.request.user.vendor_user
        if not vendor:
            raise serializers.ValidationError("Vendor profile is required.")
        super().perform_create(serializer)
        serializer.save(vendor=vendor)


class ProductDetailView(SubscriptionGateMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = ProductSerializer
    permission_classes = [IsAuthenticated, IsVerifiedVendor]
    check_product_limit = True
    # Only check image limit on updates — not on GET / DELETE
    check_image_limit = True

    def get_queryset(self):
        vendor = self.request.user.vendor_user
        if not vendor:
            raise serializers.ValidationError("Vendor profile is required.")
        return Product.objects.filter(vendor=vendor)


# ─────────────────────────────────────────────────────────────────────────────
# Bulk upload — Pro plan + explicit feature flag
# ─────────────────────────────────────────────────────────────────────────────

# Uncomment and fill in when you build the bulk upload endpoint:
#
# class BulkProductUploadView(SubscriptionGateMixin, APIView):
#     permission_classes = [
#         IsAuthenticated, IsVerifiedVendor,
#         RequireProPlan,
#         require_feature("can_access_bulk_upload"),
#     ]
#     subscription_feature = "can_access_bulk_upload"
#
#     def post(self, request):
#         ...


# ─────────────────────────────────────────────────────────────────────────────
# Discount codes — Pro plan feature flag
# ─────────────────────────────────────────────────────────────────────────────

# class DiscountCodeView(APIView):
#     permission_classes = [
#         IsAuthenticated, IsVerifiedVendor,
#         require_feature("can_offer_discounts"),
#     ]


# ─────────────────────────────────────────────────────────────────────────────
# Featured products — Pro plan feature flag
# ─────────────────────────────────────────────────────────────────────────────

# class FeatureProductView(APIView):
#     permission_classes = [
#         IsAuthenticated, IsVerifiedVendor,
#         require_feature("can_feature_products"),
#     ]


# ─────────────────────────────────────────────────────────────────────────────
# Storefront customisation — Pro plan feature flag
# ─────────────────────────────────────────────────────────────────────────────

# class StorefrontCustomisationView(APIView):
#     permission_classes = [
#         IsAuthenticated, IsVerifiedVendor,
#         require_feature("can_use_storefront_customization"),
#     ]


# ─────────────────────────────────────────────────────────────────────────────
# Everything below is unchanged from your original — no subscription gates needed
# ─────────────────────────────────────────────────────────────────────────────

class LocationAutocompleteThrottle(UserRateThrottle):
    rate = '2/second'

class LocationAutocompleteView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes   = [LocationAutocompleteThrottle]

    def get(self, request):
        query = request.query_params.get('q', '').strip()
        if not query or not re.match(r'^[\w\s,.-]+$', query):
            logger.warning(f"Invalid or empty query received: {query}")
            return Response({'error': 'Valid query parameter is required'}, status=status.HTTP_400_BAD_REQUEST)

        cache_key     = f"location_autocomplete:{query}"
        cached_result = cache.get(cache_key)
        if cached_result:
            return Response(cached_result)

        try:
            resp = requests.get(
                'https://api.locationiq.com/v1/autocomplete.php',
                params={'key': settings.LOCATIONIQ_API_KEY, 'q': query, 'format': 'json'},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return Response({'error': 'Invalid response from location service'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            cache.set(cache_key, data, timeout=3600)
            return Response(data)
        except requests.RequestException as e:
            logger.error(f"LocationIQ request failed: {e}")
            return Response({'error': 'Failed to fetch location suggestions'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# views.py
class CheckCustomerAuth(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "isAuthenticated": True,
            "email": request.user.email,
        })

class VendorSignupAPIView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        user = request.user
        if user.role == 'vendor':
            return Response({'detail': 'You already have a vendor account.'}, status=400)
        if not user.is_active:
            return Response({'detail': 'Please verify your account before applying.'}, status=400)
        serializer = VendorSignupSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            vendor = serializer.save()
            return Response({
                'message': 'Vendor signup successful. Awaiting approval.',
                'vendor_id': vendor.id,
                'slug': vendor.slug,
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class VendorDetailView(APIView):
    def get(self, request, slug):
        vendor     = get_object_or_404(Vendor, slug=slug)
        cache_key  = f"vendor_metadata:{slug}"
        cached_data = cache.get(cache_key)

        if not cached_data:
            opening_hours        = OpeningHour.objects.filter(vendor=vendor).order_by('day')
            today                = date.today().isoweekday()
            today_operating_hours = OpeningHour.objects.filter(vendor=vendor, day=today).first()
            reviews_qs           = ProductReview.objects.filter(vendor=vendor, status=True)
            avg_rating           = reviews_qs.aggregate(avg=Avg('rating'))['avg'] or 0.0
            cached_data = {
                'vendor':                VendorDetail(vendor, context={'request': request}).data,
                'average_rating':        round(avg_rating, 2),
                'review_count':          reviews_qs.count(),
                'opening_hours':         OpeningHourSerializer(opening_hours, many=True).data,
                'today_operating_hours': OpeningHourSerializer(today_operating_hours).data,
            }
            cache.set(cache_key, cached_data, timeout=60 * 60)

        self._increment_view_if_needed(request, vendor)
        return Response({
            **cached_data,
            'followers_count': vendor.followers.count(),
            'is_following': vendor.followers.filter(id=request.user.id).exists() if request.user.is_authenticated else False,
            'views': vendor.views,
        }, status=status.HTTP_200_OK)

    def post(self, request, slug):
        if not request.user.is_authenticated:
            return Response({'error': 'Please login to follow this vendor'}, status=status.HTTP_403_FORBIDDEN)
        vendor = get_object_or_404(Vendor, slug=slug)
        if vendor.followers.filter(id=request.user.id).exists():
            vendor.followers.remove(request.user)
            is_following = False
        else:
            vendor.followers.add(request.user)
            is_following = True
        return Response({'is_following': is_following, 'followers_count': vendor.followers.count()})

    def _increment_view_if_needed(self, request, vendor):
        viewed_cookie = request.headers.get('X-Recently-Viewed-Vendors', '')
        viewed_ids    = [v.strip() for v in viewed_cookie.split(',') if v.strip()]
        if str(vendor.id) not in viewed_ids:
            Vendor.objects.filter(id=vendor.id).update(views=F('views') + 1)
            vendor.refresh_from_db(fields=['views'])


class VendorProductsView(APIView):
    def get(self, request, slug):
        vendor    = get_object_or_404(Vendor, slug=slug)
        PAGE_SIZE = 6
        try:
            page = int(request.GET.get('page', 1))
        except (ValueError, TypeError):
            page = 1

        products = Product.objects.filter(vendor=vendor, status='published').annotate(
            average_rating=Avg('reviews__rating'), review_count=Count('reviews')
        ).order_by('id')

        total_items = products.count()
        total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
        page        = max(1, min(page, total_pages))
        paged       = products[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

        products_data = []
        for product in paged:
            product_variants = Variants.objects.filter(product=product)
            products_data.append({
                'product':       ProductSerializer(product, context={'request': request}).data,
                'average_rating': product.average_rating or 0,
                'review_count':   product.review_count or 0,
                'variants':       VariantsSerializer(product_variants, many=True, context={'request': request}).data,
                'colors':         list(product_variants.values('color__name', 'color__code', 'id').distinct()),
            })

        def build_url(p):
            return f"/api/v1/vendor/seller-detail/{slug}/products/?page={p}" if 1 <= p <= total_pages else None

        return Response({
            'products': products_data, 'total': total_items,
            'current_page': page, 'total_pages': total_pages,
            'next': build_url(page + 1), 'previous': build_url(page - 1) if page > 1 else None,
        })


class VendorReviewsView(APIView):
    def get(self, request, slug):
        vendor    = get_object_or_404(Vendor, slug=slug)
        cache_key = f"vendor_reviews:{slug}"
        cached    = cache.get(cache_key)
        if not cached:
            product_ids = Product.objects.filter(vendor=vendor, status='published').values_list('id', flat=True)
            reviews     = ProductReview.objects.filter(product__id__in=product_ids, status=True).order_by('-date')
            cached = {
                'reviews':        ReviewDetail(reviews, many=True, context={'request': request}).data,
                'average_rating': round(reviews.aggregate(Avg('rating'))['rating__avg'] or 0, 2),
                'review_count':   reviews.count(),
            }
            cache.set(cache_key, cached, timeout=60 * 15)
        return Response(cached)


class ProductRelatedDataAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request, *args, **kwargs):
        return Response({
            "sub_categories":   SubCategorySerializer(Sub_Category.objects.all(), many=True).data,
            "colors":           ColorSerializer(Color.objects.all(), many=True).data,
            "sizes":            SizeSerializer(Size.objects.all(), many=True).data,
            "brands":           BrandSerializer(Brand.objects.all(), many=True).data,
            "regions":          CountrySerializer(Country.objects.all(), many=True).data,
            "delivery_options": DeliveryOptionSerializer(DeliveryOption.objects.all(), many=True, context={'request': request}).data,
        })


class BankValidationView(APIView):
    def __init__(self):
        self.api_key  = settings.PAYSTACK_SECRET_KEY
        self.base_url = "https://api.paystack.co"
        self.stopwords = {'BANK','GHANA','LIMITED','LTD','PLC','AND','LOANS','SAVINGS','AFRICA','AGRICULTURAL','DEVELOPMENT'}

    def get_ghana_banks(self):
        try:
            resp = requests.get(
                f"{self.base_url}/bank?country=ghana",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=5,
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("status"):
                filtered = [b for b in data['data'] if b.get('currency') == 'GHS' and b.get('type') == 'ghipss' and b.get('active')]
                return {self._norm(b['name']): {'name': b['name'], 'code': b['code']} for b in filtered}
        except Exception as e:
            logger.error(f"Error fetching banks: {e}")
        return {}

    def _norm(self, name):
        return ' '.join(w.upper() for w in name.split() if w.upper() not in self.stopwords)

    def _abbrev(self, user_input):
        words  = [w.upper().strip() for w in re.split(r'\W+', user_input) if len(w) > 1]
        abbrevs = [w for w in words if len(w) <= 3]
        return abbrevs[0] if abbrevs else words[0] if words else user_input.upper()

    def post(self, request):
        bank_name = request.data.get('bank_name', '')
        if not bank_name:
            return Response({'error': 'Bank name is required'}, status=status.HTTP_400_BAD_REQUEST)
        ghana_banks = self.get_ghana_banks()
        if not ghana_banks:
            return Response({'error': 'Failed to fetch bank list'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        up      = bank_name.upper()
        norm    = self._norm(bank_name)
        abbrev  = self._abbrev(bank_name)

        if up in ghana_banks:
            return Response({'match': ghana_banks[up], 'score': 1.0})
        for k, info in ghana_banks.items():
            if norm == k:
                return Response({'match': info, 'score': 1.0})

        matches = difflib.get_close_matches(up, [i['name'].upper() for i in ghana_banks.values()], n=3, cutoff=0.75)
        if matches:
            best = matches[0]
            for k, info in ghana_banks.items():
                if info['name'].upper() == best:
                    score = difflib.SequenceMatcher(None, up, best).ratio()
                    return Response({'match': info, 'score': score})

        for k, info in ghana_banks.items():
            if abbrev in k:
                score = difflib.SequenceMatcher(None, abbrev, k).ratio()
                return Response({'match': info, 'score': score})

        return Response({'error': 'No matching bank found', 'suggestions': []}, status=status.HTTP_400_BAD_REQUEST)


class PayoutListView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            vendor  = Vendor.objects.get(user=request.user)
            payouts = Payout.objects.filter(vendor=vendor).order_by('-created_at')
            return Response(PayoutSerializer(payouts, many=True).data)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor profile not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error fetching payouts: {e}")
            return Response({"error": "An error occurred"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class VendorPaymentMethodAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get_queryset(self):
        return VendorPaymentMethod.objects.filter(vendor__user=self.request.user)

    def get(self, request, *args, **kwargs):
        try:
            pm = self.get_queryset().get()
            return Response(VendorPaymentMethodSerializer(pm, context={'request': request}).data)
        except ObjectDoesNotExist:
            return Response({})

    def post(self, request, *args, **kwargs):
        if self.get_queryset().exists():
            return Response({"detail": "A payment method already exists."}, status=status.HTTP_400_BAD_REQUEST)
        serializer = VendorPaymentMethodSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(vendor=request.user.vendor_user, last_updated_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, *args, **kwargs):
        try:
            instance = self.get_queryset().get()
        except ObjectDoesNotExist:
            serializer = VendorPaymentMethodSerializer(data=request.data, context={'request': request})
            if serializer.is_valid():
                serializer.save(vendor=request.user.vendor_user, last_updated_by=request.user)
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer = VendorPaymentMethodSerializer(instance, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save(last_updated_by=request.user)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class OpeningHourAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get_queryset(self):
        return OpeningHour.objects.filter(vendor__user=self.request.user)

    def get_object(self, pk):
        try:
            return self.get_queryset().get(pk=pk)
        except OpeningHour.DoesNotExist:
            raise Http404("Opening hour not found")

    def get(self, request, pk=None):
        if pk:
            return Response(OpeningHourSerializer(self.get_object(pk)).data)
        return Response(OpeningHourSerializer(self.get_queryset(), many=True).data)

    def post(self, request):
        serializer = OpeningHourSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, pk):
        serializer = OpeningHourSerializer(self.get_object(pk), data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        self.get_object(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AboutManagementAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            about = About.objects.get(vendor__user=request.user)
            return Response(AboutSerializer(about, context={'request': request}).data)
        except About.DoesNotExist:
            return Response({"detail": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    def put(self, request):
        try:
            about = About.objects.get(vendor__user=request.user)
        except About.DoesNotExist:
            return Response({"detail": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = AboutSerializer(about, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class VendorProductReviewsAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request, *args, **kwargs):
        try:
            vendor  = request.user.vendor_user
            reviews = ProductReview.objects.filter(vendor=vendor).select_related("product", "user")
            return Response(ProductReviewSerializer(reviews, many=True, context={'request': request}).data)
        except AttributeError:
            return Response({"non_field_errors": "Vendor not found."}, status=status.HTTP_404_NOT_FOUND)

    def patch(self, request, pk, *args, **kwargs):
        try:
            review     = ProductReview.objects.get(pk=pk, vendor=request.user.vendor_user)
            serializer = ProductReviewSerializer(review, data=request.data, partial=True, context={'request': request})
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except ProductReview.DoesNotExist:
            return Response({"non_field_errors": "Review not found."}, status=status.HTTP_404_NOT_FOUND)


class StandardResultsSetPagination(PageNumberPagination):
    page_size            = 10
    page_size_query_param = 'page_size'
    max_page_size        = 100


class VendorOrderListAPIView(generics.ListAPIView):
    serializer_class   = VendorOrderSerializer
    pagination_class   = StandardResultsSetPagination
    permission_classes = [IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['status', 'payment_method']
    search_fields      = ['order_number', 'user__email']
    ordering_fields    = ['date_created', 'total', 'status']
    ordering           = ['-date_created']

    def get_queryset(self):
        try:
            vendor = get_object_or_404(Vendor, user=self.request.user)
            return Order.objects.filter(vendors=vendor).select_related(
                'user', 'address'
            ).prefetch_related(
                'order_products__product',
                'order_products__selected_delivery_option',
                'order_products__product__vendor',
            )
        except Exception as e:
            logger.error(f"Error fetching vendor orders: {e}")
            raise APIException("An error occurred while fetching vendor orders.")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['vendor'] = get_object_or_404(Vendor, user=self.request.user)
        return context


class VendorOrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        try:
            vendor = Vendor.objects.get(user=request.user)
            order  = Order.objects.filter(vendors=vendor).select_related(
                'user', 'address'
            ).prefetch_related(
                'order_products__product', 'order_products__variant',
                'order_products__selected_delivery_option',
                'order_products__product__vendor',
            ).get(id=id)
            return Response(OrderSerializer(order, context={'request': request, 'vendor': vendor}).data)
        except Vendor.DoesNotExist:
            raise NotFound("Vendor account not found.")
        except Order.DoesNotExist:
            raise NotFound("Order not found.")
        except Exception as e:
            logger.error(f"Error fetching order {id}: {e}")
            raise APIException("An error occurred.")


class UpdateOrderStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def put(self, request, id):
        vendor     = get_object_or_404(Vendor, user=request.user)
        new_status = request.data.get('status')
        if new_status not in dict(Order.STATUS_CHOICES).keys():
            return Response({"error": "Invalid status choice."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            order        = Order.objects.get(id=id, vendors__in=[vendor])
            order.status = new_status
            order.save()
            return Response(OrderSerializer(order, context={'vendor': vendor, 'request': request}).data)
        except Order.DoesNotExist:
            return Response({"error": "Order not found."}, status=status.HTTP_404_NOT_FOUND)


 
from .product_detail_serializers import ProductDetailAnalyticsSerializer

class ProductAnalyticsDetailView(APIView):
    """
    Returns deep analytics for a single product owned by the authenticated vendor.
 
    - Checks that the product belongs to the requesting vendor.
    - Entirely read-only — no POST/PUT/PATCH/DELETE.
    - Uses ProductDetailAnalyticsSerializer which aggregates all metrics in one pass.
    """
    permission_classes = [IsAuthenticated, IsVerifiedVendor]
 
    def get(self, request, pk):
        # Fetch the product — 404 if it doesn't exist at all
        product = get_object_or_404(
            Product.objects.select_related(
                'sub_category', 'brand', 'vendor'
            ).prefetch_related(
                'p_images',
                'variants__size',
                'variants__color',
                'productdeliveryoption_set__delivery_option',
            ),
            pk=pk,
        )
 
        # Ownership check — 403 if the product belongs to a different vendor
        vendor = getattr(request.user, 'vendor_user', None)
        if not vendor or product.vendor != vendor:
            raise PermissionDenied("You do not have permission to view this product's analytics.")
 
        serializer = ProductDetailAnalyticsSerializer(
            product,
            context={'request': request},
        )
        return Response(serializer.data)


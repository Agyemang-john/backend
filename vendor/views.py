import datetime
from django.core.cache import cache
from datetime import date
from django.shortcuts import get_object_or_404, redirect, render
from core.models import * 
from .models import *
from userauths.models import *
from order.models import *
import json
from product.models import Product, Variants
# Create your views here.
#############################################################
#################### VENDOR #################################
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from .models import About, Vendor
from django.shortcuts import get_object_or_404
from .serializers import *
from django.db.models import Avg, Count
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404
from .models import OpeningHour, Vendor
from core.serializers import VendorSerializer as VendorDetail, ProductReviewSerializer as ReviewDetail
from rest_framework.parsers import MultiPartParser, FormParser
from django.db import transaction
from rest_framework.exceptions import NotFound
from django.db.models import Sum
from vendor.permissions import IsVerifiedVendor
from rest_framework.throttling import UserRateThrottle

from django.db.models import Sum, Count, Avg, F, Q
from django.db.models.functions import TruncDay, TruncWeek, TruncMonth, TruncDate
from datetime import timedelta
from django.utils import timezone
from order.models import OrderProduct, Order
from vendor.models import Vendor  # Assuming Vendor model
from product.models import Product, Wishlist, SavedProduct, ProductReview
from .analytics_serializers import (
    SalesSummarySerializer, SalesTrendSerializer, TopProductSerializer,
    OrderStatusSerializer, EngagementSerializer, DeliveryPerformanceSerializer
)
from vendor.models import Vendor  # Assuming Vendor model


class SalesSummaryView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        ops = OrderProduct.objects.filter(product__vendor=vendor)
        active_ops = ops.exclude(status='canceled')

        # Calculate on-time delivery rate
        delivered_ops = active_ops.filter(status='delivered', delivered_date__isnull=False)
        total_delivered = delivered_ops.count()
        on_time_count = delivered_ops.filter(
            delivered_date__lte=F('date_created') + F('selected_delivery_option__max_days') * timedelta(days=1)
        ).count()
        on_time_delivery_rate = (on_time_count / total_delivered * 100) if total_delivered > 0 else 0

        # Calculate refund rate
        refunded_ops = ops.filter(refund_reason__isnull=False).count()
        refund_rate = (refunded_ops / ops.count() * 100) if ops.count() > 0 else 0

        data = {
            'total_revenue': active_ops.aggregate(Sum('amount'))['amount__sum'] or 0,
            'total_orders': active_ops.values('order').distinct().count(),
            'total_units_sold': active_ops.aggregate(Sum('quantity'))['quantity__sum'] or 0,
            'avg_order_value': active_ops.aggregate(avg=Avg(F('amount') / F('order__id'), output_field=models.FloatField()))['avg'] or 0,
            'cancellation_rate': (ops.filter(status='canceled').count() / ops.count() * 100) if ops.count() > 0 else 0,
            'refund_rate': refund_rate,
            'on_time_delivery_rate': on_time_delivery_rate,
            'avg_rating': ProductReview.objects.filter(product__vendor=vendor).aggregate(Avg('rating'))['rating__avg'] or 0,
            'total_views': vendor.views,
            'wishlist_count': Wishlist.objects.filter(product__vendor=vendor).count(),
        }
        serializer = SalesSummarySerializer(data)
        return Response(serializer.data)

from dateutil.parser import parse  # Add this import for parsing ISO datetime strings

class SalesTrendView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        # Get query parameters
        period = request.query_params.get('period', 'day')
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')

        # Clean query parameters by removing trailing ?refresh=...
        if start_date_str and '?refresh=' in start_date_str:
            start_date_str = start_date_str.split('?refresh=')[0]
        if end_date_str and '?refresh=' in end_date_str:
            end_date_str = end_date_str.split('?refresh=')[0]

        # Parse start_date and end_date, or use defaults
        try:
            start_date = parse(start_date_str) if start_date_str else timezone.now() - timedelta(days=30)
            end_date = parse(end_date_str) if end_date_str else timezone.now()
        except (ValueError, TypeError):
            return Response({"error": "Invalid date format"}, status=status.HTTP_400_BAD_REQUEST)

        # Ensure dates are timezone-aware
        if not timezone.is_aware(start_date):
            start_date = timezone.make_aware(start_date)
        if not timezone.is_aware(end_date):
            end_date = timezone.make_aware(end_date)

        # Validate date range
        if start_date > end_date:
            return Response({"error": "start_date cannot be later than end_date"}, status=status.HTTP_400_BAD_REQUEST)

        # Query OrderProduct
        ops = OrderProduct.objects.filter(
            product__vendor=vendor,
            order__date_created__gte=start_date,
            order__date_created__lte=end_date
        ).exclude(status='canceled')

        # Aggregate data based on period
        if period == 'week':
            trend = ops.annotate(date=TruncWeek('order__date_created')).values('date').annotate(
                revenue=Sum('amount'), orders=Count('order', distinct=True)
            ).order_by('date')
        elif period == 'month':
            trend = ops.annotate(date=TruncMonth('order__date_created')).values('date').annotate(
                revenue=Sum('amount'), orders=Count('order', distinct=True)
            ).order_by('date')
        else:
            trend = ops.annotate(date=TruncDate('order__date_created')).values('date').annotate(
                revenue=Sum('amount'), orders=Count('order', distinct=True)
            ).order_by('date')

        # Format the trend data
        trend_data = [
            {
                'date': item['date'].date() if hasattr(item['date'], 'date') else item['date'],
                'revenue': float(item['revenue'] or 0),
                'orders': item['orders'] or 0,
            }
            for item in trend
        ]

        serializer = SalesTrendSerializer(trend_data, many=True)
        return Response(serializer.data)

class TopProductsView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        top_products = OrderProduct.objects.filter(
            product__vendor=vendor
        ).exclude(status='canceled').values('product__id', 'product__title').annotate(
            revenue=Sum('amount'), units_sold=Sum('quantity')
        ).order_by('-revenue')[:10]

        serializer = TopProductSerializer(top_products, many=True)
        return Response(serializer.data)

class OrderStatusView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        statuses = OrderProduct.objects.filter(product__vendor=vendor).values('status').annotate(
            count=Count('id')
        )

        serializer = OrderStatusSerializer(statuses, many=True)
        return Response(serializer.data)

class EngagementView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        data = {
            'total_views': vendor.views,
            'wishlist_count': Wishlist.objects.filter(product__vendor=vendor).count(),
            'saved_count': SavedProduct.objects.filter(product__vendor=vendor).count(),
            'review_count': ProductReview.objects.filter(product__vendor=vendor).count(),
            'avg_rating': ProductReview.objects.filter(product__vendor=vendor).aggregate(Avg('rating'))['rating__avg'] or 0,
        }
        serializer = EngagementSerializer(data)
        return Response(serializer.data)

class DeliveryPerformanceView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            vendor = Vendor.objects.get(user=request.user)
        except Vendor.DoesNotExist:
            return Response({"error": "Vendor not found"}, status=status.HTTP_404_NOT_FOUND)

        delivered_ops = OrderProduct.objects.filter(
            product__vendor=vendor, status='delivered', delivered_date__isnull=False
        ).exclude(status='canceled')
        total_delivered = delivered_ops.count()
        on_time_count = delivered_ops.filter(
            delivered_date__lte=F('date_created') + F('selected_delivery_option__max_days') * timedelta(days=1)
        ).count()
        overdue_count = delivered_ops.filter(
            delivered_date__gt=F('date_created') + F('selected_delivery_option__max_days') * timedelta(days=1)
        ).count()
        on_time_delivery_rate = (on_time_count / total_delivered * 100) if total_delivered > 0 else 0

        data = {
            'on_time_delivery_rate': on_time_delivery_rate,
            'total_delivered': total_delivered,
            'overdue_deliveries': overdue_count,
        }
        serializer = DeliveryPerformanceSerializer(data)
        return Response(serializer.data)


import requests
class LocationAutocompleteThrottle(UserRateThrottle):
    rate = '10/minute'  # Limit to 10 requests per minute per user

class LocationAutocompleteView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [LocationAutocompleteThrottle]

    def get(self, request):
        query = request.query_params.get('q', '').strip()
        
        # Sanitize query: allow alphanumeric, spaces, and common address characters
        if not query or not re.match(r'^[\w\s,.-]+$', query):
            logger.warning(f"Invalid or empty query received: {query}")
            return Response(
                {'error': 'Valid query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check cache
        cache_key = f"location_autocomplete:{query}"
        cached_result = cache.get(cache_key)
        if cached_result:
            logger.debug(f"Cache hit for query: {query}")
            return Response(cached_result)

        try:
            response = requests.get(
                'https://api.locationiq.com/v1/autocomplete.php',
                params={
                    'key': settings.LOCATIONIQ_API_KEY,
                    'q': query,
                    'format': 'json',
                },
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            # Validate response is a list (LocationIQ returns list of suggestions)
            if not isinstance(data, list):
                logger.error(f"Invalid response format from LocationIQ: {data}")
                return Response(
                    {'error': 'Invalid response from location service'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Cache result for 1 hour
            cache.set(cache_key, data, timeout=3600)
            logger.info(f"LocationIQ request successful for query: {query}")
            return Response(data)

        except requests.RequestException as e:
            logger.error(f"LocationIQ request failed: {str(e)}")
            return Response(
                {'error': 'Failed to fetch location suggestions'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

from .signup_serializers import VendorSignupSerializer
class VendorSignupAPIView(APIView):
    permission_classes = [IsAuthenticated]  # Requires logged-in user
    parser_classes = [MultiPartParser, FormParser]  # For file uploads

    def post(self, request, *args, **kwargs):
        serializer = VendorSignupSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            vendor = serializer.save()
            return Response({
                'message': 'Vendor signup successful. Awaiting approval.',
                'vendor_id': vendor.id,
                'slug': vendor.slug
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


from .serializers import CachedProductSerializer
class VendorDetailView(APIView):
    def get(self, request, slug):
        try:
            cache_key = f'vendor_detail_cache:{slug}'
            cached_data = cache.get(cache_key)

            if cached_data:
                shared_data = cached_data
            else:
                vendor = Vendor.objects.get(slug=slug)
                # Fetch published products with rating annotations
                products = Product.objects.filter(vendor=vendor, status='published').annotate(
                    average_rating=Avg('reviews__rating'),
                    review_count=Count('reviews')
                )

                # Build product details for caching
                products_with_details = []
                for product in products:
                    product_variants = Variants.objects.filter(product=product)
                    product_colors = product_variants.values('color__name', 'color__code', 'id').distinct()
                    product_data = {
                        'product': CachedProductSerializer(product, context={'request': request}).data,
                        'average_rating': product.average_rating or 0,
                        'review_count': product.review_count or 0,
                        'variants': VariantsSerializer(product_variants, many=True, context={'request': request}).data,
                        'colors': list(product_colors),
                    }
                    products_with_details.append(product_data)

                # Vendor info, reviews, and opening hours
                vendor_serializer = VendorDetail(vendor, context={'request': request})
                reviews = ProductReview.objects.filter(product__in=products, status=True).order_by("-date")
                opening_hours = OpeningHour.objects.filter(vendor=vendor).order_by('day')
                today = date.today().isoweekday()
                today_operating_hours = OpeningHour.objects.filter(vendor=vendor, day=today).first()

                shared_data = {
                    'vendor': vendor_serializer.data,
                    'products': products_with_details,
                    'average_rating': reviews.aggregate(Avg('rating'))['rating__avg'],
                    'opening_hours': OpeningHourSerializer(opening_hours, many=True, context={'request': request}).data,
                    'reviews': ReviewDetail(reviews, many=True, context={'request': request}).data,
                    'today_operating_hours': OpeningHourSerializer(today_operating_hours, context={'request': request}).data,
                    'followers_count': vendor.followers.count(),
                }

                # Cache shared data for 1 hour
                cache.set(cache_key, shared_data, timeout=60 * 60)

            # Compute fresh product details with price, old_price, and currency
            fresh_products = []
            for cached_product in shared_data['products']:
                product = Product.objects.get(id=cached_product['product']['id'])
                from .serializers import ProductSerializer

                fresh_product_data = {
                    'product': ProductSerializer(product, context={'request': request}).data,
                    'average_rating': cached_product['average_rating'],
                    'review_count': cached_product['review_count'],
                    'variants': cached_product['variants'],
                    'colors': cached_product['colors'],
                }
                fresh_products.append(fresh_product_data)

            vendor = Vendor.objects.get(slug=slug)
            vendor_id_str = str(vendor.id)
            viewed_cookie = request.headers.get('X-Recently-Viewed-Vendors')
            viewed_ids = []
            if viewed_cookie:
                try:
                    viewed_ids = [id.strip() for id in viewed_cookie.split(',') if id.strip()]
                except (ValueError, AttributeError):
                    viewed_ids = []  # Invalid format → treat as empty
            
            # Check if already viewed (no increment)
            increment_views = vendor_id_str not in viewed_ids
            # Increment if new view
            if increment_views:
                # Atomic update
                Vendor.objects.filter(id=vendor.id).update(views=F('views') + 1)
                # Refresh for latest count
                vendor.refresh_from_db(fields=['views'])
                # Optional: Log for monitoring
            # Build response with fresh is_following
            response_data = {
                **shared_data,
                'products': fresh_products,
                'is_following': Vendor.objects.filter(slug=slug, followers__id=request.user.id).exists() if request.user.is_authenticated else False,
            }

            return Response(response_data, status=status.HTTP_200_OK)

        except Vendor.DoesNotExist:
            return Response({'error': 'Vendor not found'}, status=status.HTTP_404_NOT_FOUND)

    
    def post(self, request, slug):
        if not request.user.is_authenticated:
            return Response({'error': 'Please login to follow this vendor'}, status=status.HTTP_403_FORBIDDEN)

        try:
            vendor = Vendor.objects.get(slug=slug)
        except Vendor.DoesNotExist:
            return Response({'error': 'Vendor not found'}, status=status.HTTP_404_NOT_FOUND)

        # Toggle follow/unfollow
        if vendor.followers.filter(id=request.user.id).exists():
            vendor.followers.remove(request.user)
            is_following = False
        else:
            vendor.followers.add(request.user)
            is_following = True

        response_data = {
            'is_following': is_following,
            'followers_count': vendor.followers.count(),
        }

        return Response(response_data, status=status.HTTP_200_OK)

class VendorProducts(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user, if exists."""
        return get_object_or_404(Vendor, user=request.user)
    
    def get(self, request, *args, **kwargs):
        vendor = self.get_vendor(request)
        # Retrieve the product associated with the vendor
        products = Product.objects.filter(vendor=vendor)      
        # Serialize each queryset
        products_serializer = ProductSerializer(products, many=True, context={'request': request})
        # Combine all serialized data into a single response
        data = {
            "products": products_serializer.data,
        }

        return Response(data)
    
class ProductRelatedDataAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request, *args, **kwargs):
        sub_categories = Sub_Category.objects.all()
        brands = Brand.objects.all()
        countries = Country.objects.all()
        colors = Color.objects.all()
        sizes = Size.objects.all()
        categories = Category.objects.all()  # Assuming you have a Category model
        delivery_options = DeliveryOption.objects.all()

        # Serialize each queryset
        color_serializer = ColorSerializer(colors, many=True)
        size_serializer = SizeSerializer(sizes, many=True)
        sub_category_serializer = SubCategorySerializer(sub_categories, many=True)
        brand_serializer = BrandSerializer(brands, many=True)
        region_serializer = CountrySerializer(countries, many=True)
        delivery_options_serializer = DeliveryOptionSerializer(delivery_options, many=True, context={'request': request}).data

        # Combine all serialized data into a single response
        data = {
            "sub_categories": sub_category_serializer.data,
            "colors": color_serializer.data,
            "sizes": size_serializer.data,
            "brands": brand_serializer.data,
            "regions": region_serializer.data,
            "delivery_options": delivery_options_serializer,
        }

        return Response(data)


from rest_framework import generics, permissions
from product.models import Product
from .product_serializers import ProductSerializer

class IsVendorOwner(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return hasattr(request.user, 'vendor') and obj.vendor == request.user.vendor


class ProductListCreateView(generics.ListCreateAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsVerifiedVendor, IsAuthenticated]

    def perform_create(self, serializer):
        vendor = self.request.user.vendor_user
        if not vendor:  
            raise serializers.ValidationError("Vendor profile is required. Please complete your vendor setup.")
        serializer.save(vendor=vendor)


class ProductCreateView(generics.CreateAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsVerifiedVendor, IsAuthenticated]

class ProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsVerifiedVendor, IsAuthenticated]

# PAYMENT METHOD VIEWSET
from .models import VendorPaymentMethod
from .payment_serializers import VendorPaymentMethodSerializer, PayoutSerializer
from django.core.exceptions import ObjectDoesNotExist
import logging
import difflib


class BankValidationView(APIView):
    def __init__(self):
        self.api_key = settings.PAYSTACK_SECRET_KEY
        self.base_url = "https://api.paystack.co"
        self.stopwords = {'BANK', 'GHANA', 'LIMITED', 'LTD', 'PLC', 'AND', 'LOANS', 'SAVINGS', 'AFRICA', 'AGRICULTURAL', 'DEVELOPMENT'}

    def get_ghana_banks(self):
        """Fetch list of Ghanaian banks from Paystack API."""
        url = f"{self.base_url}/bank?country=ghana"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        try:
            response = requests.get(url, headers=headers)
            response_data = response.json()
            if response.status_code == 200 and response_data.get("status"):
                filtered_banks = [
                    bank for bank in response_data['data']
                    if bank.get('currency') == 'GHS' and bank.get('type') == 'ghipss' and bank.get('active')
                ]
                banks = {
                    self.normalize_bank_name(bank['name']): {'name': bank['name'], 'code': bank['code']}
                    for bank in filtered_banks
                }
                logger.info(f"Fetched {len(banks)} GHS banks for validation")
                return banks
            else:
                logger.error(f"Failed to fetch banks: {response_data.get('message')}")
                return {}
        except Exception as e:
            logger.error(f"Error fetching banks: {e}")
            return {}

    def normalize_bank_name(self, name):
        """Normalize bank name: upper, remove stopwords, strip."""
        words = [word.upper() for word in name.split() if word.upper() not in self.stopwords]
        return ' '.join(words)

    def extract_keywords(self, user_input):
        """Extract key words/abbreviations from user input (e.g., 'ADB Bank' -> ['ADB'])."""
        words = [word.upper().strip() for word in re.split(r'\W+', user_input) if len(word) > 1]
        abbrevs = [w for w in words if len(w) <= 3]
        return abbrevs[0] if abbrevs else words[0] if words else user_input.upper()

    def post(self, request):
        bank_name = request.data.get('bank_name', '')
        if not bank_name:
            return Response({'error': 'Bank name is required'}, status=status.HTTP_400_BAD_REQUEST)

        ghana_banks = self.get_ghana_banks()
        if not ghana_banks:
            return Response({'error': 'Failed to fetch bank list'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        user_name_upper = bank_name.upper()
        normalized_user = self.normalize_bank_name(bank_name)
        key_abbrev = self.extract_keywords(bank_name)

        # Exact match
        if user_name_upper in ghana_banks:
            return Response({'match': ghana_banks[user_name_upper], 'score': 1.0}, status=status.HTTP_200_OK)
        for norm_key, bank_info in ghana_banks.items():
            if normalized_user == norm_key:
                return Response({'match': bank_info, 'score': 1.0}, status=status.HTTP_200_OK)

        # Fuzzy match
        matches = difflib.get_close_matches(user_name_upper, [info['name'].upper() for info in ghana_banks.values()], n=3, cutoff=0.75)
        if matches:
            best_match = matches[0]
            for norm_key, bank_info in ghana_banks.items():
                if bank_info['name'].upper() == best_match:
                    score = difflib.SequenceMatcher(None, user_name_upper, best_match).ratio()
                    logger.info(f"Fuzzy match for '{bank_name}': '{best_match}' (score: {score:.2f})")
                    return Response({'match': bank_info, 'score': score}, status=status.HTTP_200_OK)

        # Abbreviation fallback
        for norm_key, bank_info in ghana_banks.items():
            if key_abbrev in norm_key:
                score = difflib.SequenceMatcher(None, key_abbrev, norm_key).ratio()
                logger.info(f"Abbrev match for '{bank_name}': '{bank_info['name']}' (score: {score:.2f})")
                return Response({'match': bank_info, 'score': score}, status=status.HTTP_200_OK)

        logger.warning(f"No match found for bank name: '{bank_name}'")
        return Response({'error': 'No matching bank found', 'suggestions': []}, status=status.HTTP_400_BAD_REQUEST)

# Viewing seller payouts
from payments.models import Payout
class PayoutListView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get(self, request):
        try:
            # Get the vendor associated with the authenticated user
            vendor = Vendor.objects.get(user=request.user)
            # Fetch payouts for the vendor
            payouts = Payout.objects.filter(vendor=vendor).order_by('-created_at')
            serializer = PayoutSerializer(payouts, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Vendor.DoesNotExist:
            logger.error(f"No vendor found for user {request.user.id}")
            return Response(
                {"error": "Vendor profile not found"},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error fetching payouts for user {request.user.id}: {str(e)}")
            return Response(
                {"error": "An error occurred while fetching payouts"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class VendorPaymentMethodAPIView(APIView):
    permission_classes = [IsVerifiedVendor, IsAuthenticated]

    def get_queryset(self):
        return VendorPaymentMethod.objects.filter(vendor__user=self.request.user)

    def get(self, request, *args, **kwargs):
        try:
            payment_method = self.get_queryset().get()
            serializer = VendorPaymentMethodSerializer(payment_method, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ObjectDoesNotExist:
            return Response({}, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        if self.get_queryset().exists():
            return Response(
                {"detail": "A payment method already exists for this vendor."},
                status=status.HTTP_400_BAD_REQUEST
            )
        serializer = VendorPaymentMethodSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(vendor=self.request.user.vendor_user, last_updated_by=self.request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, *args, **kwargs):
        try:
            instance = self.get_queryset().get()
        except ObjectDoesNotExist:
            serializer = VendorPaymentMethodSerializer(data=request.data, context={'request': request})
            if serializer.is_valid():
                serializer.save(vendor=self.request.user.vendor_user, last_updated_by=self.request.user)
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer = VendorPaymentMethodSerializer(instance, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save(last_updated_by=self.request.user)
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


from .models import OpeningHour
from .hour_serializers import OpeningHourSerializer
from django.http import Http404

class OpeningHourAPIView(APIView):
    permission_classes = [IsVerifiedVendor, IsAuthenticated]

    def get_queryset(self):
        """Return queryset filtered by the authenticated vendor."""
        return OpeningHour.objects.filter(vendor__user=self.request.user)

    def get_object(self, pk):
        """Retrieve a single OpeningHour instance for the authenticated vendor."""
        try:
            return self.get_queryset().get(pk=pk)
        except OpeningHour.DoesNotExist:
            raise Http404("Opening hour not found")

    def get(self, request, pk=None):
        """
        Handle GET requests:
        - If pk is provided, retrieve a single OpeningHour.
        - Otherwise, list all OpeningHours for the vendor.
        """
        if pk:
            opening_hour = self.get_object(pk)
            serializer = OpeningHourSerializer(opening_hour)
            return Response(serializer.data)
        opening_hours = self.get_queryset()
        serializer = OpeningHourSerializer(opening_hours, many=True)
        return Response(serializer.data)

    def post(self, request):
        """Handle POST requests to create a new OpeningHour."""
        serializer = OpeningHourSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, pk):
        """Handle PUT requests to update an existing OpeningHour."""
        opening_hour = self.get_object(pk)
        serializer = OpeningHourSerializer(opening_hour, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        """Handle DELETE requests to remove an OpeningHour."""
        opening_hour = self.get_object(pk)
        opening_hour.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ABOUT VIEWSET
from .models import About
from .about_serializers import AboutSerializer
from django.http import Http404

class AboutManagementAPIView(APIView):
    permission_classes = [IsVerifiedVendor, IsAuthenticated]

    def get(self, request):
        try:
            about = About.objects.get(vendor__user=request.user)
            serializer = AboutSerializer(about, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)
        except About.DoesNotExist:
            return Response({"detail": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    def put(self, request):
        try:
            about = About.objects.get(vendor__user=request.user)
            serializer = AboutSerializer(about, data=request.data, context={'request': request})
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except About.DoesNotExist:
            return Response({"detail": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)


from .product_serializers import ProductReviewSerializer
class VendorProductReviewsAPIView(APIView):
    permission_classes = [IsVerifiedVendor, IsAuthenticated]

    def get(self, request, *args, **kwargs):
        """List all reviews for the logged-in vendor's products"""
        try:
            vendor = request.user.vendor_user
            reviews = ProductReview.objects.filter(vendor=vendor).select_related("product", "user")
            serializer = ProductReviewSerializer(reviews, many=True, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)
        except AttributeError:
            return Response({"non_field_errors": "Vendor not found for this user."}, status=status.HTTP_404_NOT_FOUND)

    def patch(self, request, pk, *args, **kwargs):
        """Update review status"""
        try:
            review = ProductReview.objects.get(pk=pk, vendor=request.user.vendor_user)
            serializer = ProductReviewSerializer(review, data=request.data, partial=True, context={'request': request})
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except ProductReview.DoesNotExist:
            return Response({"non_field_errors": "Review not found or you are not authorized to update it."}, status=status.HTTP_404_NOT_FOUND)

# from rest_framework import generics, status
# from rest_framework.pagination import PageNumberPagination
# from rest_framework.filters import SearchFilter, OrderingFilter
# from django_filters.rest_framework import DjangoFilterBackend
# from order.models import Order
# from .order_serializers import VendorOrderSerializer
# from django.shortcuts import get_object_or_404

# class StandardResultsSetPagination(PageNumberPagination):
#     page_size = 10  # Matches frontend INITIAL_PAGE_SIZE
#     page_size_query_param = 'page_size'
#     max_page_size = 100

# class VendorOrderListAPIView(generics.ListAPIView):
#     permission_classes = [IsVerifiedVendor, IsAuthenticated]
#     """
#     APIView to list orders for the authenticated vendor.
#     Supports pagination, sorting, and filtering.
#     """
#     serializer_class = VendorOrderSerializer
#     pagination_class = StandardResultsSetPagination
#     permission_classes = [IsAuthenticated]
#     filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
#     filterset_fields = ['status', 'payment_method']  # Filter by status, payment method
#     search_fields = ['order_number', 'user__email']  # Search by order number, user email
#     ordering_fields = ['date_created', 'total', 'status']  # Sortable fields
#     ordering = ['-date_created']  # Default ordering

#     def get_queryset(self):
#         user = self.request.user
#         vendor = get_object_or_404(Vendor, user=user)
#         return Order.objects.filter(vendors=vendor).select_related('user', 'address').prefetch_related('order_products__product', 'order_products__selected_delivery_option')

from rest_framework import generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.exceptions import NotFound, APIException
from order.models import Order
from .order_serializers import VendorOrderSerializer
from vendor.models import Vendor
from django.shortcuts import get_object_or_404
import logging

logger = logging.getLogger(__name__)

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100

class VendorOrderListAPIView(generics.ListAPIView):
    serializer_class = VendorOrderSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'payment_method']
    search_fields = ['order_number', 'user__email']
    ordering_fields = ['date_created', 'total', 'status']
    ordering = ['-date_created']

    def get_queryset(self):
        try:
            vendor = get_object_or_404(Vendor, user=self.request.user)
            return Order.objects.filter(vendors=vendor).select_related(
                'user', 'address'
            ).prefetch_related(
                'order_products__product',
                'order_products__selected_delivery_option',
                'order_products__product__vendor'
            )
        except Vendor.DoesNotExist:
            logger.warning(f"Vendor not found for user {self.request.user.id}")
            raise NotFound("Vendor account not found.")
        except Exception as e:
            logger.error(f"Error fetching vendor orders for user {self.request.user.id}: {str(e)}")
            raise APIException("An error occurred while fetching vendor orders.")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['vendor'] = get_object_or_404(Vendor, user=self.request.user)
        return context

from .order_serializers import OrderSerializer

class VendorOrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        try:
            vendor = Vendor.objects.get(user=request.user)
            order = Order.objects.filter(vendors=vendor).select_related(
                'user', 'address'
            ).prefetch_related(
                'order_products__product',
                'order_products__variant',
                'order_products__selected_delivery_option',
                'order_products__product__vendor'
            ).get(id=id)
            serializer = OrderSerializer(
                order,
                context={'request': request, 'vendor': vendor}
            )
            return Response(serializer.data)
        except Vendor.DoesNotExist:
            logger.warning(f"Vendor not found for user {request.user.id}")
            raise NotFound("Vendor account not found.")
        except Order.DoesNotExist:
            logger.warning(f"Order {id} not found for vendor {request.user.id}")
            raise NotFound("Order not found.")
        except Exception as e:
            logger.error(f"Error fetching vendor order {id} for user {request.user.id}: {str(e)}")
            raise APIException("An error occurred while fetching the order.")

class UpdateOrderStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor]

    def get_vendor(self, request):
        return get_object_or_404(Vendor, user=request.user)

    def put(self, request, id):
        vendor = self.get_vendor(request)
        new_status = request.data.get('status')
        valid_status_choices = dict(Order.STATUS_CHOICES).keys()
        if new_status not in valid_status_choices:
            return Response(
                {"error": "Invalid status choice."},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            order = Order.objects.get(id=id, vendors__in=[vendor])
            order.status = new_status
            order.save()
            serializer = OrderSerializer(order, context={'vendor': vendor, 'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found."},
                status=status.HTTP_404_NOT_FOUND
            )

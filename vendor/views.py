import datetime
from django.core.cache import cache
from datetime import date
from django.shortcuts import get_object_or_404, redirect, render
from core.models import * 
from .models import *
from userauths.models import *
from order.models import *
from django.contrib import messages
from django.db import IntegrityError
from django.http import JsonResponse, HttpResponse
from userauths.utils import is_vendor
from .utils import get_vendor
import json
from django.shortcuts import render, get_object_or_404, redirect
from product.models import Product, ProductImages, Variants, VariantImage
from django.contrib.auth.decorators import login_required, user_passes_test

# Create your views here.
#############################################################
#################### VENDOR #################################
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from .models import About, Vendor
from .serializers import AboutSerializer
from django.shortcuts import get_object_or_404
from .permissions import IsVendor
from .serializers import *
from django.db.models import Avg, Count
from django.db.models.functions import TruncMonth
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from .models import OpeningHour, Vendor
from core.serializers import VendorSerializer as VendorDetail, ProductReviewSerializer as ReviewDetail
from rest_framework.parsers import MultiPartParser, FormParser
from django.db import transaction
from rest_framework.exceptions import NotFound
from django.db.models import Sum


class VendorSignUpView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated]

    def get_vendor(self, request):
        vendor = Vendor.objects.filter(user=request.user).first()
        return vendor

    def post(self, request, *args, **kwargs):
        existing_vendor = self.get_vendor(request)
        if existing_vendor:
            return Response(
                {
                    "error": "User is already associated with a vendor",
                    "details": {
                        "storeName": existing_vendor.name,
                        "email": existing_vendor.email,
                        "contact": existing_vendor.contact,
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Vendor-related data
        vendor_data = {
            "name": request.data.get("storeName"),
            "email": request.data.get("businessEmail"),
            "contact": request.data.get("phoneNumber"),
            "country": request.data.get("country"),
            "business_type": request.data.get("businessType"),
            "vendor_type": request.data.get("vendorType"),
            "license": request.data.get("license"),
            "student_id": request.data.get("studentId"),
        }

        try:
            # Use a transaction to ensure atomicity
            with transaction.atomic():
                # Validate and save vendor
                vendor_serializer = VendorRegistrationSerializer(data=vendor_data)
                if not vendor_serializer.is_valid():
                    print(vendor_serializer.errors)
                    return Response(
                        {"error": "Vendor validation failed", "details": vendor_serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                vendor = vendor_serializer.save(user=request.user)

                # Ensure the About instance is created
                about, created = About.objects.get_or_create(vendor=vendor)

                # Update the About instance
                about.profile_image = request.data.get("profilePicture")
                about.cover_image = request.data.get("coverImage")
                about.address = request.data.get("businessAddress")
                about.about = request.data.get("about")
                about.latitude = request.data.get("latitude")
                about.longitude = request.data.get("longitude")
                about.save()

                # Payment method data
                payment_data = {
                    "vendor": vendor.id,
                    "payment_method": request.data.get("paymentMethod"),
                    "momo_number": request.data.get("momoNumber"),
                    "momo_provider": request.data.get("momoProvider"),
                    "bank_name": request.data.get("bankName"),
                    "bank_account_name": request.data.get("bankAccountName"),
                    "bank_account_number": request.data.get("bankAccountNumber"),
                    "bank_routing_number": request.data.get("bankRoutingNumber"),
                    "country": request.data.get("country"),
                }

                # Validate and save payment details
                payment_serializer = VendorPaymentMethodSerializer(data=payment_data)
                if not payment_serializer.is_valid():
                    return Response(
                        {"error": "Payment method validation failed", "details": payment_serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                payment_serializer.save(vendor=vendor)

        except Exception as e:
            return Response(
                {"error": "Something went wrong during vendor registration", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"message": "Vendor registered successfully!"},
            status=status.HTTP_201_CREATED,
        )


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

                # Vendor info
                vendor_serializer = VendorDetail(vendor, context={'request': request})
                
                # Reviews and average rating
                reviews = ProductReview.objects.filter(product__in=products, status=True).order_by("-date")
                average_rating = reviews.aggregate(Avg('rating'))['rating__avg']

                # Build product details
                products_with_details = []
                for product in products:
                    product_variants = Variants.objects.filter(product=product)
                    product_colors = product_variants.values('color__name', 'color__code', 'sku').distinct()

                    product_data = {
                        'product': ProductSerializer(product, context={'request': request}).data,
                        'average_rating': product.average_rating or 0,
                        'review_count': product.review_count or 0,
                        'variants': VariantsSerializer(product_variants, many=True, context={'request': request}).data,
                        'colors': list(product_colors),
                    }
                    products_with_details.append(product_data)

                # Operating hours for today
                today = date.today().isoweekday()
                today_operating_hours = OpeningHour.objects.filter(vendor=vendor, day=today).first()

                shared_data = {
                    'vendor': vendor_serializer.data,
                    'products': products_with_details,
                    'average_rating': average_rating,
                    'reviews': ReviewDetail(reviews, many=True, context={'request': request}).data,
                    'today_operating_hours': OpeningHourSerializer(today_operating_hours, context={'request': request}).data,
                    'followers_count': vendor.followers.count(),
                }

                # Cache shared data for 10 minutes
                cache.set(cache_key, shared_data, timeout=600)

        except Vendor.DoesNotExist:
            return Response({'error': 'Vendor not found'}, status=status.HTTP_404_NOT_FOUND)

        # User-specific logic (do not cache)
        is_following = False
        if request.user.is_authenticated:
            is_following = Vendor.objects.filter(slug=slug, followers__id=request.user.id).exists()

        response_data = {
            **shared_data,
            'is_following': is_following,
        }

        return Response(response_data, status=status.HTTP_200_OK)

    
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


class VendorPaymentMethodAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVendor]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user, if exists."""
        return get_object_or_404(Vendor, user=request.user)
   
    def get(self, request, *args, **kwargs):
        """
        Retrieve the payment method for the authenticated vendor.
        """
        vendor = self.get_vendor(request) # Assuming vendor is linked to the user model
        payment_method = VendorPaymentMethod.objects.filter(vendor=vendor).first()

        if not payment_method:
            return Response({"detail": "Payment method not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = VendorPaymentMethodSerializer(payment_method)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        """
        Create a payment method for the authenticated vendor.
        """
        vendor = self.get_vendor(request) # Assuming vendor is linked to the user model
        
        # Check if the vendor already has a payment method
        if VendorPaymentMethod.objects.filter(vendor=vendor).exists():
            return Response(
                {"detail": "Vendor already has a payment method. Use PUT to update."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        data = request.data
        data['vendor'] = vendor.id  # Ensure vendor is linked to the authenticated user
        serializer = VendorPaymentMethodSerializer(data=data)
        
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, *args, **kwargs):
        """
        Update the payment method for the authenticated vendor.
        """
        vendor = self.get_vendor(request)

        payment_method = get_object_or_404(VendorPaymentMethod, vendor=vendor)

        data = request.data
        serializer = VendorPaymentMethodSerializer(payment_method, data=data, partial=True)
        
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class OpeningHourDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user, if exists."""
        return get_object_or_404(Vendor, user=request.user)
    
    def get(self, request, *args, **kwargs):
        vendor = self.get_vendor(request)
        opening_hours = OpeningHour.objects.filter(vendor=vendor)
        serializer = OpeningHourSerializer(opening_hours, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        vendor = self.get_vendor(request)
        # serializer = OpeningHourSerializer(data=request.data)
        serializer = OpeningHourSerializer(data=request.data, context={'request': request})
        
        if serializer.is_valid():
            serializer.save(vendor=vendor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        print(serializer.errors)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, pk=None, *args, **kwargs):
        """Fully update an existing opening hour entry."""
        vendor = self.get_vendor(request)
        opening_hour = get_object_or_404(OpeningHour, pk=pk, vendor=vendor)
        serializer = OpeningHourSerializer(opening_hour, data=request.data, context={'request': request})

        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    def patch(self, request, pk=None, *args, **kwargs):
        """Partially update an existing opening hour entry."""
        vendor = self.get_vendor(request)
        opening_hour = get_object_or_404(OpeningHour, pk=pk, vendor=vendor)
        serializer = OpeningHourSerializer(opening_hour, data=request.data, partial=True)

        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk=None, *args, **kwargs):
        """Delete an existing opening hour entry."""
        vendor = self.get_vendor(request)
        opening_hour = get_object_or_404(OpeningHour, pk=pk, vendor=vendor)
        opening_hour.delete()
        return Response({"detail": "Opening hour deleted successfully."}, status=status.HTTP_204_NO_CONTENT)

class UpdateOrderProductStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVendor]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user."""
        return get_object_or_404(Vendor, user=request.user)

    def put(self, request, id):
        """
        Allow a vendor to update the status of their products in an order.
        """
        vendor = self.get_vendor(request)

        # Retrieve the OrderProduct
        order_product = get_object_or_404(
            OrderProduct.objects.filter(product__vendor=vendor),  # Ensure vendor ownership
            id=id
        )

        # Update the status
        status_value = request.data.get("status")
        if status_value not in dict(OrderProduct._meta.get_field('status').choices):
            return Response({"error": "Invalid status value"}, status=status.HTTP_400_BAD_REQUEST)

        order_product.status = status_value
        order_product.save()

        # Serialize and return the updated object
        serializer = OrderProductSerializer(order_product)
        return Response(serializer.data, status=status.HTTP_200_OK)


class UpdateOrderStatusAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVendor]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user, if exists."""
        return get_object_or_404(Vendor, user=request.user)

    def put(self, request, order_id):
        vendor = self.get_vendor(request)
        # Get the status from the request data
        new_status = request.data.get('status')
        
        # Validate that the status is one of the allowed choices
        valid_status_choices = dict(Order.STATUS_CHOICES).keys()
        if new_status not in valid_status_choices:
            return Response(
                {"error": "Invalid status choice."},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            # Retrieve the order based on the order_id
            order = Order.objects.get(id=order_id)
            
            # Update the status and save the order
            order.status = new_status
            order.save()

            # Serialize and return the updated order data
            serializer = OrderSerializer(order, context={'vendor': vendor})
            return Response(serializer.data, status=status.HTTP_200_OK)
        
        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found."},
                status=status.HTTP_404_NOT_FOUND
            )

class OrderDetailsAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVendor]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user, if exists."""
        return get_object_or_404(Vendor, user=request.user)

    def get(self, request, id):
        try:
            vendor = self.get_vendor(request)
            order = Order.objects.get(id=id, vendors__in=[vendor])  # Ensure the vendor is part of the order
            serializer = OrderSerializer(order, context={'vendor': vendor})  # Pass vendor context to the serializer
            return Response(serializer.data)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)


class VendorAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVendor]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user, if exists."""
        return get_object_or_404(Vendor, user=request.user)

    def get(self, request, format=None):
        """
        GET: Retrieve all orders for the authenticated vendor.
        """
        vendor = self.get_vendor(request)

        # Fetch products for the vendor
        products = Product.objects.filter(vendor=vendor, status='published').annotate(
            average_rating=Avg('reviews__rating'),
            review_count=Count('reviews')
        )

        # Fetch orders associated with the vendor
        orders = Order.objects.filter(vendors=vendor)
        vendor_serializer = VendorSerializer(vendor, context={'request': request})
        product_serializer = ProductSerializer(products, many=True, context={'request': request})

        # Get orders by month
        orders_by_month = (
            Order.objects.filter(vendors=vendor)
            .annotate(month=TruncMonth("date_created"))
            .values("month")
            .annotate(order_count=Count("id"))
            .order_by("month")
        )

        # Get reviews for the vendor's products
        reviews = ProductReview.objects.filter(product__in=products, status=True).order_by("-date")
        average_rating = reviews.aggregate(Avg('rating'))['rating__avg']
        order_serializer = OrderSerializer(orders, many=True, context={'vendor': vendor}, )

        # Calculate product sales
        product_sales = []
        for product in products:
            sales_count = (
                OrderProduct.objects.filter(order__in=orders, product=product)
                .aggregate(total_sales=Sum('quantity'))['total_sales'] or 0
            )
            product_sales.append({
                'product': ProductSerializer(product, context={'request': request}).data,
                'sales_count': sales_count
            })

        # Response data
        response_data = {
            'vendor': vendor_serializer.data,
            'products': product_serializer.data,
            'orders': order_serializer.data,
            'average_rating': average_rating,
            "reviews": ProductReviewSerializer(reviews, many=True, context={'request': request}).data,
            'followers_count': vendor.followers.count(),
            'orders_by_month': list(orders_by_month),
            'product_sales': product_sales,  # Add product sales data
        }

        return Response(response_data, status=status.HTTP_200_OK)

class AboutAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVendor]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user, if exists."""
        return get_object_or_404(Vendor, user=request.user)

    def get(self, request, format=None):
        """
        GET: Retrieve the About instance for the authenticated vendor or list all instances if not a vendor.
        """
        # If the user is authenticated and is a vendor, retrieve their About instance
        # if request.user.is_authenticated and hasattr(request.user, 'vendor_user'):
        vendor = self.get_vendor(request)
        about = get_object_or_404(About, vendor=vendor)
        serializer = AboutSerializer(about, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, format=None):
        """
        POST: Create a new About instance for the authenticated vendor.
        """
        vendor = self.get_vendor(request)
        serializer = AboutSerializer(data=request.data, )
        if serializer.is_valid():
            serializer.save(vendor=vendor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, format=None):
        """
        PUT: Update the About instance for the authenticated vendor.
        """        
        # Get the authenticated vendor
        vendor = self.get_vendor(request)
        
        # Fetch the About instance for the vendor or return 404 if not found
        about = get_object_or_404(About, vendor=vendor)
        
        # Use the serializer with partial updates
        serializer = AboutSerializer(about, data=request.data, partial=True)
        
        # Check if the serializer data is valid
        if serializer.is_valid():
            # Save the validated data
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        # If there are validation errors, print them for debugging
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


    def delete(self, request, format=None):
        """
        DELETE: Remove the About instance for the authenticated vendor.
        """
        vendor = self.get_vendor(request)
        about = get_object_or_404(About, vendor=vendor)
        about.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class VendorProducts(APIView):
    permission_classes = [IsAuthenticated, IsVendor]

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
    permission_classes = [IsAuthenticated, IsVendor]

    def get(self, request, *args, **kwargs):
        sub_categories = Sub_Category.objects.all()
        brands = Brand.objects.all()
        regions = Region.objects.all()
        categories = Category.objects.all()  # Assuming you have a Category model
        delivery_options = DeliveryOption.objects.all()

        # Serialize each queryset
        sub_category_serializer = SubCategorySerializer(sub_categories, many=True)
        brand_serializer = BrandSerializer(brands, many=True)
        region_serializer = RegionSerializer(regions, many=True)
        delivery_options_serializer = DeliveryOptionSerializer(delivery_options, many=True, context={'request': request}).data

        # Combine all serialized data into a single response
        data = {
            "sub_categories": sub_category_serializer.data,
            "brands": brand_serializer.data,
            "regions": region_serializer.data,
            "delivery_options": delivery_options_serializer,
        }

        return Response(data)

class VendorProductAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVendor]

    def get_vendor(self, request):
        """Retrieve the vendor associated with the current user, if exists."""
        return get_object_or_404(Vendor, user=request.user)
    
    def get(self, request, product_id, *args, **kwargs):
        vendor = self.get_vendor(request)
        
        # Retrieve the product associated with the vendor
        product = get_object_or_404(Product, id=product_id, vendor=vendor)
        
        # Retrieve related images and delivery options for the product
        images = ProductImages.objects.filter(product=product)
        delivery_options = ProductDeliveryOption.objects.filter(product=product)

        # Serialize product details
        product_serializer = ProductSerializer(product, context={"request": request})
        
        # Serialize images and delivery options separately
        images_serializer = ProductImagesSerializer(images, many=True)
        delivery_options_serializer = ProductDeliveryOptionSerializer(delivery_options, many=True)

        # Construct response data to include all serialized information
        response_data = {
            "product": product_serializer.data,
            "images": images_serializer.data,
            "delivery_options": delivery_options_serializer.data
        }

        return Response(response_data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        vendor = self.get_vendor(request)
        main_image = request.FILES.get('image')
        additional_images = request.FILES.getlist('images[]')
        serializer = ProductSerializer(data=request.data)

        if serializer.is_valid():
            product = serializer.save(vendor=vendor)

            if main_image:
                product.image = main_image
                product.save()

            for image in additional_images:
                ProductImages.objects.create(product=product, images=image)
            
            # Handle delivery options
            delivery_options = request.data.getlist('delivery_options')
            for option_str in delivery_options:
                try:
                    option = json.loads(option_str)  # Parse each option from JSON string
                    delivery_option = DeliveryOption.objects.get(id=option['deliveryOptionId'])
                    ProductDeliveryOption.objects.create(
                        product=product,
                        delivery_option=delivery_option,
                        default=option.get('default', False)
                    )
                except (json.JSONDecodeError, KeyError, DeliveryOption.DoesNotExist) as e:
                    return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        
        print("Validation errors:", serializer.errors)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    def put(self, request, product_id, *args, **kwargs):
        vendor = self.get_vendor(request)
        # Retrieve the product to be updated
        product = get_object_or_404(Product, id=product_id, vendor=vendor)

        # Extract images from the request
        main_image = request.FILES.get('image')
        additional_images = request.FILES.getlist('images[]')
        deleted_images = request.data.get('deletedImages', []) #this should be the ids of images deleted in the frontend

        print(deleted_images)

        if deleted_images:
            ProductImages.objects.filter(id__in=deleted_images, product=product).delete()

        # Initialize serializer with existing product instance and incoming data
        serializer = ProductSerializer(product, data=request.data, partial=True)

        if serializer.is_valid():
            # Update the product with new data
            product = serializer.save()

            # Update the main image if provided
            if main_image:
                product.image = main_image
                product.save()

            # Remove old additional images and save new ones
            if additional_images:
                ProductImages.objects.filter(product=product).delete()
                for image in additional_images:
                    ProductImages.objects.create(product=product, images=image)

            # Update delivery options
            delivery_options = request.data.getlist('delivery_options')
            print(delivery_options)
            if delivery_options:
                ProductDeliveryOption.objects.filter(product=product).delete()  # Clear existing options
                for option_str in delivery_options:
                    try:
                        option = json.loads(option_str)  # Parse each option from JSON string
                        delivery_option = DeliveryOption.objects.get(id=option['deliveryOptionId'])
                        ProductDeliveryOption.objects.create(
                            product=product,
                            delivery_option=delivery_option,
                            default=option.get('default', False)
                        )
                    except (json.JSONDecodeError, KeyError, DeliveryOption.DoesNotExist) as e:
                        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            print(serializer.data)
            return Response(serializer.data, status=status.HTTP_200_OK)

        # Log and return validation errors
        print("Validation errors:", serializer.errors)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, product_id, *args, **kwargs):
        vendor = self.get_vendor(request)
        # Retrieve the product to be updated
        product = get_object_or_404(Product, id=product_id, vendor=vendor)

        # Optionally, you can add additional checks for the vendor to ensure they have the right to delete the product
        vendor = self.get_vendor(request)
        if product.vendor != vendor:
            return Response({"error": "You do not have permission to delete this product."}, status=status.HTTP_403_FORBIDDEN)
        
        # Delete related images (if any)
        product_images = ProductImages.objects.filter(product=product)
        product_images.delete()

        # Delete related delivery options (if any)
        ProductDeliveryOption.objects.filter(product=product).delete()

        # Delete the product itself
        product.delete()
        return Response({"message": "Product and related data successfully deleted."}, status=status.HTTP_204_NO_CONTENT)


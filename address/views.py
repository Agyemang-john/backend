from rest_framework import status
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from .serializers import *
from userauths.models import Profile
from order.service import *
# User = get_user_model()
from django.db.models.deletion import ProtectedError
from rest_framework.generics import RetrieveUpdateDestroyAPIView
from rest_framework.exceptions import NotFound, ValidationError
from django.db import transaction
from .serializers import AddressSerializer
import logging
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from address.models import Address

logger = logging.getLogger(__name__)

class AddressListCreateView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AddressSerializer

    def get_queryset(self):
        # Return addresses for the authenticated user
        return Address.objects.filter(user=self.request.user)

    def get(self, request):
        # List addresses for the logged-in user
        addresses = self.get_queryset()
        serializer = self.serializer_class(addresses, many=True)
        return Response(serializer.data)

    def post(self, request):
        # Create a new address
        serializer = self.serializer_class(data=request.data)
        if serializer.is_valid():
            with transaction.atomic():
                # If status=True, unset other default addresses for the user
                if serializer.validated_data.get('status'):
                    Address.objects.filter(user=self.request.user, status=True).update(status=False)
                serializer.save(user=self.request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class AddressDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AddressSerializer
    lookup_field = 'id'  # Use 'id' to look up addresses

    def get_queryset(self):
        # Only return addresses belonging to the authenticated user
        return Address.objects.filter(user=self.request.user)

    def get_object(self):
        # Override to add logging and custom error message
        try:
            obj = super().get_object()
            logger.info(f"Retrieved address {obj.id} for user {self.request.user.id}")
            return obj
        except Address.DoesNotExist:
            logger.error(f"Address with id {self.kwargs['id']} not found for user {self.request.user.id}")
            raise NotFound(f"Address with ID {self.kwargs['id']} not found.")

    def perform_update(self, serializer):
        address_obj = self.get_object()
        data = serializer.validated_data
        user = self.request.user

        # Auto-geocode if latitude/longitude are missing
        if not data.get('latitude') or not data.get('longitude'):
            query = ', '.join(filter(None, [data.get('town', ''), data.get('region', ''), data.get('country', '')]))
            try:
                response = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"format": "json", "q": query, "addressdetails": 1, "limit": 1},
                    headers={"User-Agent": "Negromart (support@negromart.com)"},
                    timeout=10
                )
                response.raise_for_status()
                geocoded = response.json()
                if geocoded:
                    lat = float(geocoded[0]['lat'])
                    lon = float(geocoded[0]['lon'])
                    serializer.save(user=user, latitude=lat, longitude=lon)
                    return
            except Exception as e:
                logger.error(f"Geocoding failed: {e}")

        # If status is true, reset others
        if data.get('status', False):
            Address.objects.filter(user=user, status=True).exclude(pk=address_obj.pk).update(status=False)

        serializer.save(user=user)
        logger.info(f"Updated address {address_obj.id} for user {user.id}")

    def perform_destroy(self, obj):
        # Log deletion and perform delete
        logger.info(f"Deleting address {obj.id} for user {self.request.user.id}")
        super().perform_destroy(obj)

    def retrieve(self, request, *args, **kwargs):
        # Wrap response in a consistent format
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response({
            'status': 'success',
            'message': 'Address retrieved successfully.',
            'data': serializer.data
        }, status=status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        # Wrap response and handle errors
        try:
            response = super().update(request, *args, **kwargs)
            return Response({
                'status': 'success',
                'message': 'Address updated successfully.',
                'data': response.data
            }, status=status.HTTP_200_OK)
        except ValidationError as e:
            logger.error(f"Validation error updating address {self.kwargs['id']}: {e.detail}")
            return Response({
                'status': 'error',
                'message': 'Failed to update address.',
                'errors': e.detail
            }, status=status.HTTP_400_BAD_REQUEST)

    def destroy(self, request, *args, **kwargs):
        try:
            response = super().destroy(request, *args, **kwargs)
            return Response({
                'status': 'success',
                'message': 'Address deleted successfully.',
                'data': None
            }, status=status.HTTP_204_NO_CONTENT)
        except ProtectedError as e:
            logger.error(f"Cannot delete address {self.kwargs['id']} due to associated orders: {e}")
            return Response({
                'status': 'error',
                'message': 'Cannot delete this address because it is associated with one or more orders.',
                'data': None
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Error deleting address {self.kwargs['id']}: {e}")
            return Response({
                'status': 'error',
                'message': 'An unexpected error occurred while deleting the address.',
                'data': None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


from django.db import transaction

class MakeDefaultAddressView(APIView):
    permission_classes = [IsAuthenticated]
    def put(self, request):
        address_id = request.data.get('id')
        if not address_id:
            return Response({"error": "Address ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                address = Address.objects.get(id=address_id, user=request.user)
                Address.objects.filter(user=request.user).update(status=False)
                address.status = True
                address.save()

                profile, created = Profile.objects.get_or_create(user=request.user)
                profile.address = address.address
                profile.country = address.country
                profile.mobile = address.mobile
                profile.latitude = address.latitude
                profile.longitude = address.longitude
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
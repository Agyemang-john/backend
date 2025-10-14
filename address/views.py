from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from .serializers import *
from userauths.models import Profile
from order.service import *
# User = get_user_model()
from rest_framework.generics import GenericAPIView

from rest_framework.generics import RetrieveUpdateDestroyAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from .serializers import AddressSerializer
from address.models import Address
import logging
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from .serializers import AddressSerializer
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
        data = serializer.validated_data
        if not data.get('latitude') and not data.get('longitude'):
            query = ', '.join(filter(None, [
                # data.get('address', ''),
                data.get('town', ''),
            ]))
            try:
                response = requests.get(
                    f"https://nominatim.openstreetmap.org/search?format=json&q={query}&addressdetails=1&limit=1"
                )
                response.raise_for_status()
                geocoded = response.json()
                if geocoded:
                    data['latitude'] = float(geocoded[0]['lat'])
                    data['longitude'] = float(geocoded[0]['lon'])
                    data['address'] = float(geocoded[0]['display_name'])
            except Exception as e:
                logger.error(f"Geocoding failed: {e}")
                # Optionally allow null coordinates

        user = self.request.user
        status = serializer.validated_data.get('status', False)
        if status:  # If user sets this address as default
            Address.objects.filter(user=user, status=True).exclude(pk=self.get_object().pk).update(status=False)

        serializer.save(user=self.request.user)
        logger.info(f"Updated address {self.get_object().id} for user {self.request.user.id}")

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
        # Wrap response for deletion
        response = super().destroy(request, *args, **kwargs)
        return Response({
            'status': 'success',
            'message': 'Address deleted successfully.',
            'data': None
        }, status=status.HTTP_204_NO_CONTENT)


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
from django.core.cache import cache
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from .serializers import *
from userauths.tokens import otp_token_generator
from userauths.utils import send_sms
from userauths.models import Profile, User
from order.service import *
# User = get_user_model()
from rest_framework.decorators import api_view, permission_classes
from rest_framework.generics import GenericAPIView


class AddressListCreateView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AddressSerializer

    def get(self, request):
        # List addresses for the logged-in user
        addresses = Address.objects.filter(user=request.user)
        serializer = self.serializer_class(addresses, many=True)
        return Response(serializer.data)

    def post(self, request):
        # Create a new address
        serializer = self.serializer_class(data=request.data)
        if serializer.is_valid():
            serializer.save(user=request.user)  # Associate address with the user
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class AddressDetailView(APIView):
    permission_classes = [IsAuthenticated]
    # Retrieve, update, or delete an address
    def get_object(self, request, id):
        try:
            return Address.objects.get(id=id, user=request.user)
        except Address.DoesNotExist:
            raise KeyError

    def get(self, request, id):
        address = self.get_object(request, id)
        serializer = AddressSerializer(address)
        return Response(serializer.data)

    def put(self, request, id):
        address = self.get_object(request, id)
        serializer = AddressSerializer(address, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save(user=request.user)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, id):
        address = self.get_object(request, id)
        address.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
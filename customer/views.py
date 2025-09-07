from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from userauths.models import Profile
from .serializers import *
from product.models import  *
from order.models import *
from rest_framework.exceptions import NotFound


class ProfileAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        """Retrieve the authenticated user's profile."""
        try:
            # Use select_related for optimization if the user field is frequently accessed
            profile = Profile.objects.select_related('user').get(user=request.user)
            serializer = ProfileSerializer(profile, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Profile.DoesNotExist:
            return Response({"detail": "Profile not found."}, status=status.HTTP_404_NOT_FOUND)

    def post(self, request, *args, **kwargs):
        """Create a new profile for the authenticated user."""
        data = request.data.copy()
        data['user'] = request.user.id  # Associate the profile with the authenticated user
        serializer = ProfileSerializer(data=data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        print(serializer.errors)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, *args, **kwargs):
        """Update the authenticated user's profile."""
        try:
            profile = Profile.objects.get(user=request.user)
            serializer = ProfileSerializer(profile, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Profile.DoesNotExist:
            return Response({"detail": "Profile not found."}, status=status.HTTP_404_NOT_FOUND)


class UserOrdersView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        orders = Order.objects.filter(user=request.user)
        serializer = OrderSerializer(orders, many=True)
        return Response(serializer.data)

class OrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        try:
            order = Order.objects.get(id=id, user=request.user)
        except Order.DoesNotExist:
            raise NotFound("Order not found.")

        serializer = OrderSerializer(order, context={'request': request})
        return Response(serializer.data)


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response({"message": "Password updated successfully."}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserReviewsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        reviews = ProductReview.objects.filter(user=user)
        serializer = ProductReviewSerializer(reviews, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class WishlistAPIView(APIView):
    def get(self, request):
        wishlists = Wishlist.objects.filter(user=request.user).order_by('-saved_at')
        serializer = WishlistSerializer(wishlists, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = WishlistSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        wishlist = Wishlist.objects.filter(user=request.user, id=pk).first()
        if wishlist:
            wishlist.delete()
            return Response({'message': 'Product removed from wishlist.'}, status=status.HTTP_204_NO_CONTENT)
        return Response({'error': 'Wishlist item not found.'}, status=status.HTTP_404_NOT_FOUND)

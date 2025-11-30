# apps/newsletters/views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import Subscriber
from .serializers import SubscriberSerializer
from .tasks import send_subscription_confirmation_email

class SubscribeAPIView(APIView):
    """
    Step 1: User enters email → backend saves inactive subscriber and emails confirm link.
    """
    def post(self, request):
        serializer = SubscriberSerializer(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data["email"].lower().strip()

            defaults = serializer.validated_data.copy()

            if request.user.is_authenticated:
                defaults.setdefault("first_name", request.user.first_name)
                defaults.setdefault("last_name", request.user.last_name)

            subscriber, created = Subscriber.objects.get_or_create(
                email=email,
                defaults=serializer.validated_data
            )

            # Case 1: Already confirmed
            if subscriber.is_active:
                return Response(
                    {"status": "already_active", "message": "You are already subscribed 🎉"},
                    status=status.HTTP_200_OK
                )

            # Case 2: Exists but not yet confirmed
            if not created and not subscriber.is_active:
                subscriber.generate_tokens()
                send_subscription_confirmation_email.delay(subscriber.id, subscriber.confirm_token)
                return Response(
                    {"status": "pending", "message": "You’ve already subscribed but not confirmed. We resent the confirmation email 📩"},
                    status=status.HTTP_200_OK
                )

            # Case 3: New subscription (just created)
            if created:
                subscriber.generate_tokens()
                send_subscription_confirmation_email.delay(subscriber.id, subscriber.confirm_token)
                return Response(
                    {"status": "new", "message": "Confirmation email sent. Please check your inbox."},
                    status=status.HTTP_201_CREATED
                )

        return Response(
            {"status": "error", "message": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST
        )



class ConfirmAPIView(APIView):
    """
    Step 2: User clicks confirm link → backend marks them active.
    """
    def post(self, request, token):
        try:
            subscriber = Subscriber.objects.get(confirm_token=token)
            subscriber.mark_confirmed()
            return Response({"message": "Subscription confirmed!"}, status=status.HTTP_200_OK)
        except Subscriber.DoesNotExist:
            return Response({"error": "Invalid token"}, status=status.HTTP_400_BAD_REQUEST)


class UnsubscribeAPIView(APIView):
    """
    Step 3: User clicks unsubscribe link → backend marks them inactive.
    """
    def post(self, request, token):
        try:
            subscriber = Subscriber.objects.get(unsubscribe_token=token)
            subscriber.mark_unsubscribed()
            return Response({"message": "You have been unsubscribed."}, status=status.HTTP_200_OK)
        except Subscriber.DoesNotExist:
            return Response({"error": "Invalid token"}, status=status.HTTP_400_BAD_REQUEST)

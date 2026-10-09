# notification/views.py — FINAL WORKING VERSION
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .serializers import OrderSerializer
from order.models import Order

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_websocket_token(request):
    # request.auth is the AccessToken OBJECT → convert to string!
    return Response({"token": str(request.auth)})


from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView

from .models import Notification
from .serializers import NotificationSerializer

class NotificationListView(generics.ListAPIView):
    """
    GET /api/v1/notification/list/
    Returns all notifications for the authenticated user.
    """
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user).order_by("-created_at")


class NotificationMarkReadView(APIView):
    """
    POST /api/v1/notification/<id>/mark-read/
    Marks a single notification as read.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        try:
            notif = Notification.objects.get(id=id, recipient=request.user)
        except Notification.DoesNotExist:
            return Response({"detail": "Notification not found"}, status=status.HTTP_404_NOT_FOUND)
        
        notif.mark_as_read()
        serializer = NotificationSerializer(notif)
        return Response(serializer.data, status=status.HTTP_200_OK)


class NotificationMarkAllReadView(APIView):
    """
    POST /api/v1/notification/mark-all-read/
    Marks all notifications for the authenticated user as read.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response({"detail": "All notifications marked as read"}, status=status.HTTP_200_OK)

class NotificationDeleteView(APIView):
    """
    DELETE /api/v1/notification/<id>/delete/
    Deletes a single notification.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, id):
        try:
            notif = Notification.objects.get(id=id, recipient=request.user)
        except Notification.DoesNotExist:
            return Response({"detail": "Notification not found"}, status=status.HTTP_404_NOT_FOUND)

        notif.delete()
        return Response({"detail": "Notification deleted"}, status=status.HTTP_204_NO_CONTENT)


class NotificationDetailView(APIView):
    """
    GET /api/v1/notification/<id>/
    Returns notification details and marks it as read.
    """
    permission_classes = [IsAuthenticated]
    def get(self, request, id):
        try:
            notif = Notification.objects.get(id=id, recipient=request.user)
        except Notification.DoesNotExist:
            return Response({"detail": "Notification not found"}, status=status.HTTP_404_NOT_FOUND)

        if not notif.is_read:
            notif.mark_as_read()

        serializer = NotificationSerializer(notif)
        return Response(serializer.data, status=status.HTTP_200_OK)

from rest_framework.permissions import IsAuthenticated, AllowAny
from .serializers import ContactInquirySerializer, ReportSerializer


class ContactInquiryCreateView(APIView):
    def post(self, request):
        serializer = ContactInquirySerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            inquiry = serializer.save()
            return Response({
                "message": "Thank you! Your message has been received.",
                "ticket_id": inquiry.supportticket.ticket_id
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ReportCreateView(APIView):
    permission_classes = [IsAuthenticated]  # Only logged-in users can report

    def post(self, request):
        serializer = ReportSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            report = serializer.save()
            return Response({
                "message": "Report submitted successfully. Thank you!",
                "report_id": str(report.id)
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class OrderListView(ListAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).order_by('-date_created')



from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from .models import ContactInquiry, SupportTicket, TicketReply
def send_reply_view(request, inquiry_id):
    if not request.user.is_staff:
        messages.error(request, "Access denied.")
        return redirect('/admin/')

    inquiry = get_object_or_404(ContactInquiry, id=inquiry_id)

    if request.method == 'POST':
        reply_msg = request.POST.get('reply_message', '').strip()
        internal_note = request.POST.get('internal_note', '').strip()

        # The reverse accessor of SupportTicket.inquiry is `supportticket`
        # (no related_name); inquiries created before tickets existed have none.
        ticket, _ = SupportTicket.objects.get_or_create(inquiry=inquiry)

        if reply_msg:
            # Saving a public reply queues the customer email through the
            # TicketReply post_save signal (notification.tasks.send_ticket_reply_email),
            # which also marks the inquiry resolved once the email is sent.
            # Mailing here as well sent the customer two copies.
            TicketReply.objects.create(
                ticket=ticket,
                replied_by=request.user,
                message=reply_msg,
                is_internal=False
            )
            messages.success(request, f"Reply queued for {inquiry.email}.")

        if internal_note:
            TicketReply.objects.create(
                ticket=ticket,
                replied_by=request.user,
                message=internal_note,
                is_internal=True
            )

        return redirect(f"/dashboard/negromart/notification/contactinquiry/{inquiry_id}/change/")

    return redirect('/dashboard/negromart/')

# notification/tasks.py
from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.utils.html import strip_tags
from django.template.loader import render_to_string
import logging

logger = logging.getLogger(__name__)

@shared_task(
    bind=True,
    max_retries=5,
    default_retry_delay=60,  # 1 minute
    autoretry_for=(ConnectionError, TimeoutError),
)
def send_ticket_reply_email(self, reply_id):
    from .models import TicketReply

    try:
        reply = TicketReply.objects.select_related(
            'ticket__inquiry'
        ).get(id=reply_id)

        if reply.is_internal:
            return "Skipped: internal note"

        inquiry = reply.ticket.inquiry
        customer_email = inquiry.email
        customer_name = inquiry.name or "Customer"

        subject = f"Re: {inquiry.subject} (Ticket #{reply.ticket.ticket_id})"

        context = {
            'customer_name': customer_name,
            'reply_message': reply.message,
            'ticket_id': reply.ticket.ticket_id,
            'original_message': inquiry.message,
            'support_email': settings.DEFAULT_FROM_EMAIL,
        }

        html_message = render_to_string('admin/ticket_reply.html', context)
        plain_message = strip_tags(html_message)

        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[customer_email],
            html_message=html_message,
            fail_silently=False,
        )

        # Mark as resolved only after successful send
        inquiry.replied_at = reply.created_at
        inquiry.status = 'resolved'
        inquiry.save(update_fields=['replied_at', 'status'])

        logger.info(f"Ticket reply email sent to {customer_email}")
        return f"Email sent to {customer_email}"

    except TicketReply.DoesNotExist:
        logger.warning(f"TicketReply {reply_id} not found")
        return "Reply not found"
    except Exception as exc:
        logger.error(f"Failed to send ticket reply email: {exc}")
        raise self.retry(exc=exc)
from celery import shared_task
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings
import logging
from userauths.arkesel_client import ArkeselSMS
sms_client = ArkeselSMS()

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3, retry_backoff=True)
def send_vendor_approval_email(self, vendor_id, is_approved):
    """
    Celery task to send approval or denial email to vendor asynchronously.
    
    Args:
        vendor_id (int): ID of the Vendor instance
        is_approved (bool): Approval status of the vendor
    """
    try:
        from .models import Vendor
        vendor = Vendor.objects.get(id=vendor_id)
        
        subject = (
            "Congratulations! Your shop has been approved"
            if is_approved
            else "We're sorry! Your shop is not eligible"
        )
        template = (
            'email/store-approval-email.html'
            if is_approved
            else 'email/store-denied-email.html'
        )
        
        context = {
            'user': vendor.user,
            'is_approved': is_approved,
            'to_email': vendor.email,
            'vendor_name': vendor.name
        }
        
        email_message = render_to_string(template, context)
        from_email = settings.DEFAULT_FROM_EMAIL or 'ecommerceplatform35@gmail.com'
        
        email = EmailMessage(
            subject=subject,
            body=email_message,
            from_email=from_email,
            to=[vendor.email]
        )
        email.content_subtype = 'html'
        email.send()
        
        logger.info(f"Approval email sent to {vendor.email} (Approved: {is_approved})")
        
    except Vendor.DoesNotExist:
        logger.error(f"Vendor with id {vendor_id} not found")
        raise self.retry(countdown=60)
    except Exception as e:
        logger.error(f"Failed to send email to vendor {vendor_id}: {str(e)}")
        raise self.retry(exc=e, countdown=60)

@shared_task(bind=True, max_retries=3, retry_backoff=True)
def send_vendor_sms(self, vendor_id, is_approved):
    """
    Celery task to send SMS notification to vendor asynchronously.
    
    Args:
        vendor_id (int): ID of the Vendor instance
        is_approved (bool): Approval status of the vendor
    """
    try:
        from .models import Vendor
        vendor = Vendor.objects.get(id=vendor_id)
        message = (
            "Congratulations! Your Negromart shop has been approved. Log in at https://seller.negromart.com/auth/login to get started."
            if is_approved
            else "We're sorry! Your Negromart shop is not eligible. Contact support@negromart.com for details."
        )

        response = sms_client.send_sms(
            sender=settings.ARKESEL_SENDER,
            message=message,
            recipients=[vendor.contact]
        )
        
        if response.get('status') == 'success':
            logger.info(f"SMS sent to {vendor.contact} (Approved: {is_approved}): {response}")
            return response
        else:
            logger.error(f"Failed to send SMS to {vendor.contact}: {response}")
            raise Exception(f"Arkesel API error: {response.get('message', 'Unknown error')}")
            
    except Vendor.DoesNotExist:
        logger.error(f"Vendor with id {vendor_id} not found")
        raise self.retry(countdown=60)
    except Exception as e:
        logger.error(f"SMS sending failed for vendor {vendor_id}: {str(e)}")
        raise self.retry(exc=e, countdown=60)
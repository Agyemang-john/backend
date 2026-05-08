from celery import shared_task
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings
import logging
from userauths.arkesel_client import ArkeselSMS
sms_client = ArkeselSMS()

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0, soft_time_limit=300, time_limit=360)
def process_bulk_upload(self, job_id, rows, vendor_id):
    """
    Async Celery task for large bulk product uploads (>100 rows).
    Processes rows, creates products, updates SubscriptionUsage, and
    writes the result back to the BulkUploadJob record.
    """
    from .models import BulkUploadJob, Vendor
    from vendor.bulk_upload_serializer import BulkProductRowSerializer
    from payments.models import SubscriptionUsage
    from django.db import transaction
    from django.db.models import F

    try:
        job = BulkUploadJob.objects.get(id=job_id)
        vendor = Vendor.objects.get(id=vendor_id)

        job.status = BulkUploadJob.STATUS_PROCESSING
        job.save(update_fields=["status", "updated_at"])

        created_ids = []
        errors = []

        for i, row in enumerate(rows, start=2):
            serializer = BulkProductRowSerializer(data=row)
            if not serializer.is_valid():
                errors.append({
                    "row": i,
                    "title": row.get("title", "—"),
                    "errors": serializer.errors,
                })
                continue
            try:
                with transaction.atomic():
                    product = serializer.save(vendor=vendor)
                    created_ids.append(product.id)
            except Exception as exc:
                logger.error(
                    "BulkUpload job %s: row %d failed for vendor %d: %s",
                    job_id, i, vendor_id, exc, exc_info=True,
                )
                errors.append({
                    "row": i,
                    "title": row.get("title", "—"),
                    "errors": {"non_field_errors": [str(exc)]},
                })

        if created_ids:
            SubscriptionUsage.objects.filter(vendor=vendor).update(
                active_products_count=F("active_products_count") + len(created_ids)
            )

        job.status = BulkUploadJob.STATUS_DONE
        job.success_count = len(created_ids)
        job.failed_count = len(errors)
        job.created_product_ids = created_ids
        job.errors = errors
        job.save(update_fields=[
            "status", "success_count", "failed_count",
            "created_product_ids", "errors", "updated_at",
        ])

        logger.info(
            "BulkUpload job %s done: %d created, %d failed",
            job_id, len(created_ids), len(errors),
        )

    except BulkUploadJob.DoesNotExist:
        logger.error("BulkUploadJob %s not found", job_id)
    except Exception as exc:
        logger.error("BulkUpload job %s crashed: %s", job_id, exc, exc_info=True)
        BulkUploadJob.objects.filter(id=job_id).update(
            status=BulkUploadJob.STATUS_FAILED,
            error_message=str(exc),
        )

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
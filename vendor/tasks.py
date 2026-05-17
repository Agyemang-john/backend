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


# ── Vendor view analytics tasks ──────────────────────────────────────────────

@shared_task(ignore_result=True)
def log_vendor_view_event(vendor_id, visitor_key, user_id, is_bot, is_returning, device_type, date_str):
    """Write one VendorViewLog row. Called async so the store response is never blocked."""
    from datetime import date as _date
    from .models import VendorViewLog
    try:
        VendorViewLog.objects.create(
            vendor_id=vendor_id,
            visitor_key=visitor_key,
            user_id=user_id,
            is_bot=is_bot,
            is_returning=is_returning,
            device_type=device_type,
            date=_date.fromisoformat(date_str),
        )
    except Exception as e:
        logger.error("log_vendor_view_event: failed vendor=%s err=%s", vendor_id, e)


@shared_task(ignore_result=True)
def flush_vendor_view_counts():
    """
    Drain Redis vendor view-count buffers (vendor:views:buf:{id}) into Vendor.views.
    Runs every 3 minutes via Celery Beat — same pattern as product flush_view_counts.
    """
    try:
        from django_redis import get_redis_connection
        conn = get_redis_connection("default")
    except Exception:
        logger.warning("flush_vendor_view_counts: Redis unavailable, skipping")
        return

    updates: dict[int, int] = {}
    cursor = 0

    while True:
        try:
            cursor, keys = conn.scan(cursor, match="vendor:views:buf:*", count=500)
        except Exception as e:
            logger.error("flush_vendor_view_counts: SCAN error: %s", e)
            break

        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            try:
                vendor_id = int(key.split(":")[-1])
            except ValueError:
                continue
            try:
                try:
                    raw = conn.getdel(key)
                except AttributeError:
                    pipe = conn.pipeline()
                    pipe.get(key)
                    pipe.delete(key)
                    raw, _ = pipe.execute()
                if raw:
                    updates[vendor_id] = updates.get(vendor_id, 0) + int(raw)
            except Exception as e:
                logger.error("flush_vendor_view_counts: error on key %s: %s", key, e)

        if cursor == 0:
            break

    if not updates:
        return

    from django.db import transaction
    from django.db.models import F
    from .models import Vendor
    with transaction.atomic():
        for vendor_id, delta in updates.items():
            Vendor.objects.filter(id=vendor_id).update(views=F('views') + delta)

    logger.info("flush_vendor_view_counts: flushed %d vendor(s), total delta %d",
                len(updates), sum(updates.values()))


@shared_task(ignore_result=True)
def aggregate_vendor_daily_stats():
    """
    Materialise yesterday's VendorViewLog rows into VendorDailyStats.
    Runs at 00:10 UTC via Celery Beat.
    """
    from django.utils import timezone as tz
    from django.db.models import Count, Q
    from datetime import timedelta as _td
    from .models import VendorViewLog, VendorDailyStats

    yesterday = (tz.now() - _td(days=1)).date()
    rows = list(
        VendorViewLog.objects
        .filter(date=yesterday)
        .values('vendor_id')
        .annotate(
            total=Count('id'),
            unique=Count('id', filter=Q(is_returning=False, is_bot=False)),
            returning=Count('id', filter=Q(is_returning=True, is_bot=False)),
            bots=Count('id', filter=Q(is_bot=True)),
        )
    )
    for row in rows:
        VendorDailyStats.objects.update_or_create(
            vendor_id=row['vendor_id'],
            date=yesterday,
            defaults={
                'total_views':     row['total'],
                'unique_views':    row['unique'],
                'returning_views': row['returning'],
                'bot_views':       row['bots'],
            },
        )
    logger.info("aggregate_vendor_daily_stats: aggregated %d vendor(s) for %s", len(rows), yesterday)
from celery import shared_task
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

# Lazy SMS client — instantiated on first use so a missing ARKESEL_API_KEY
# doesn't prevent task registration on worker startup.
_sms_client = None

def _get_sms_client():
    global _sms_client
    if _sms_client is None:
        from userauths.arkesel_client import ArkeselSMS
        _sms_client = ArkeselSMS()
    return _sms_client


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
        logger.error("send_vendor_approval_email: vendor %s not found — aborting", vendor_id)
        return  # no point retrying a missing row
    except Exception as e:
        logger.error("send_vendor_approval_email: vendor %s error: %s", vendor_id, e)
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

        response = _get_sms_client().send_sms(
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
        logger.error("send_vendor_sms: vendor %s not found — aborting", vendor_id)
        return  # no point retrying a missing row
    except Exception as e:
        logger.error("send_vendor_sms: vendor %s error: %s", vendor_id, e)
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


# ── Vendor activity tracking tasks ───────────────────────────────────────────

@shared_task(bind=True, max_retries=3, retry_backoff=True, ignore_result=True)
def notify_shop_status_change(self, vendor_id, paused):
    """
    Send email, SMS, and in-app notification when a seller manually
    pauses or resumes their shop.
    """
    from .models import Vendor
    from notification.models import Notification

    try:
        vendor = Vendor.objects.select_related('user').get(pk=vendor_id)
        email  = vendor.email or vendor.user.email
        name   = vendor.name

        if paused:
            verb     = 'vendor_shop_paused'
            notif_msg = (
                'Your shop has been paused. Your products are now hidden from customers. '
                'Log in to your dashboard and toggle it back on when you\'re ready.'
            )
            subject  = 'Your Negromart shop has been paused'
            template = 'email/vendor-shop-paused.html'
            sms_body = (
                f"Negromart: Your shop '{name}' is now paused. Customers cannot see your "
                f"products. Toggle it back on anytime: https://seller.negromart.com/auth/login"
            )
        else:
            verb     = 'vendor_shop_resumed'
            notif_msg = (
                'Your shop is live again! Your products are visible to customers and '
                'they can search, add to cart, and place orders.'
            )
            subject  = 'Your Negromart shop is live again!'
            template = 'email/vendor-shop-resumed.html'
            sms_body = (
                f"Negromart: Great news! Your shop '{name}' is live. "
                f"Customers can now find and order your products."
            )

        # In-app notification (non-critical — never blocks email/SMS)
        try:
            Notification.objects.create(
                recipient=vendor.user,
                verb=verb,
                data={'message': notif_msg},
            )
        except Exception as notif_exc:
            logger.error("notify_shop_status_change: in-app notif failed vendor=%s err=%s", vendor_id, notif_exc)

        # Email — isolated so SMTP failure never silences the SMS
        email_ok = False
        try:
            context = {
                'vendor': vendor,
                'vendor_name': name,
                'user': vendor.user,
                'to_email': email,
                'login_url': 'https://seller.negromart.com/auth/login',
                'paused': paused,
            }
            body = render_to_string(template, context)
            msg  = EmailMessage(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[email],
            )
            msg.content_subtype = 'html'
            msg.send()
            email_ok = True
        except Exception as email_exc:
            logger.error("notify_shop_status_change: email failed vendor=%s err=%s", vendor_id, email_exc)

        # SMS — isolated so it always runs regardless of email outcome
        sms_ok = False
        if vendor.contact:
            try:
                response = _get_sms_client().send_sms(
                    sender=settings.ARKESEL_SENDER,
                    message=sms_body,
                    recipients=[vendor.contact],
                )
                if response.get('status') == 'success':
                    sms_ok = True
                else:
                    logger.warning(
                        "notify_shop_status_change: SMS not confirmed vendor=%s resp=%s",
                        vendor_id, response,
                    )
            except Exception as sms_exc:
                logger.error("notify_shop_status_change: SMS failed vendor=%s err=%s", vendor_id, sms_exc)
        else:
            sms_ok = True  # no contact number — not an error

        logger.info(
            "notify_shop_status_change: vendor=%s paused=%s email=%s sms=%s",
            vendor_id, paused, email_ok, sms_ok,
        )

        # Retry only if BOTH channels failed (at least one delivered is acceptable)
        if not email_ok and not sms_ok and vendor.contact:
            raise self.retry(
                exc=Exception("Both email and SMS failed for vendor %s" % vendor_id),
                countdown=60,
            )

    except Vendor.DoesNotExist:
        logger.error("notify_shop_status_change: vendor %s not found", vendor_id)
    except Exception as exc:
        if not hasattr(exc, 'is_celery_max_retries'):
            logger.error("notify_shop_status_change: vendor=%s unexpected err=%s", vendor_id, exc)
        raise


@shared_task(ignore_result=True)
def log_vendor_activity(vendor_id, event_type, ip_address=None, user_agent='', metadata=None):
    """Write one VendorActivityLog row. Always called async so views stay fast."""
    from .models import VendorActivityLog
    try:
        VendorActivityLog.objects.create(
            vendor_id=vendor_id,
            event_type=event_type,
            ip_address=ip_address,
            user_agent=user_agent or '',
            metadata=metadata or {},
        )
    except Exception as e:
        logger.error("log_vendor_activity: vendor=%s event=%s err=%s", vendor_id, event_type, e)


@shared_task(ignore_result=True)
def flush_vendor_last_seen():
    """
    Drain Redis `vendor:last_seen:{id}` keys into Vendor.last_seen_at.
    Runs every 5 minutes via Celery Beat.
    """
    try:
        from django_redis import get_redis_connection
        conn = get_redis_connection("default")
    except Exception:
        logger.warning("flush_vendor_last_seen: Redis unavailable, skipping")
        return

    from django.utils import timezone as tz
    from datetime import datetime, timezone as dt_timezone
    from .models import Vendor

    updates: dict[int, int] = {}
    cursor = 0

    while True:
        try:
            cursor, keys = conn.scan(cursor, match="vendor:last_seen:*", count=500)
        except Exception as e:
            logger.error("flush_vendor_last_seen: SCAN error: %s", e)
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
                    updates[vendor_id] = max(updates.get(vendor_id, 0), int(raw))
            except Exception as e:
                logger.error("flush_vendor_last_seen: error on key %s: %s", key, e)

        if cursor == 0:
            break

    if not updates:
        return

    for vendor_id, ts in updates.items():
        try:
            last_seen = datetime.fromtimestamp(ts, tz=dt_timezone.utc)
            Vendor.objects.filter(pk=vendor_id).update(last_seen_at=last_seen)
        except Exception as e:
            logger.error("flush_vendor_last_seen: DB update failed vendor=%s err=%s", vendor_id, e)

    logger.info("flush_vendor_last_seen: updated %d vendor(s)", len(updates))


@shared_task(bind=True, max_retries=3, retry_backoff=True, ignore_result=True)
def send_vendor_inactivity_sms(self, vendor_id, message):
    """Send a short inactivity SMS to the vendor's contact number via Arkesel."""
    try:
        from .models import Vendor
        vendor = Vendor.objects.get(pk=vendor_id)
        contact = vendor.contact
        if not contact:
            logger.warning("send_vendor_inactivity_sms: vendor %s has no contact number", vendor_id)
            return
        response = _get_sms_client().send_sms(
            sender=settings.ARKESEL_SENDER,
            message=message,
            recipients=[contact],
        )
        if response.get('status') == 'success':
            logger.info("send_vendor_inactivity_sms: sent to vendor %s (%s)", vendor_id, contact)
        else:
            raise Exception(f"Arkesel error: {response.get('message', response)}")
    except Exception as exc:
        from .models import Vendor as _V
        if isinstance(exc, _V.DoesNotExist):
            logger.error("send_vendor_inactivity_sms: vendor %s not found — aborting", vendor_id)
            return
        logger.error("send_vendor_inactivity_sms: vendor=%s err=%s", vendor_id, exc)
        raise self.retry(exc=exc, countdown=120)


@shared_task(ignore_result=True)
def send_vendor_inactivity_email(vendor_id, template, subject, extra_context=None):
    """Send an inactivity-related email to a vendor."""
    from .models import Vendor
    try:
        vendor = Vendor.objects.select_related('user').get(pk=vendor_id)
        context = {
            'vendor': vendor,
            'user': vendor.user,
            'vendor_name': vendor.name,
            'to_email': vendor.email or vendor.user.email,
            'inactivity_days': getattr(settings, 'VENDOR_INACTIVITY_DAYS', 30),
            'login_url': 'https://seller.negromart.com/auth/login',
            **(extra_context or {}),
        }
        body = render_to_string(template, context)
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[vendor.email or vendor.user.email],
        )
        email.content_subtype = 'html'
        email.send()
        logger.info("send_vendor_inactivity_email: sent %s to vendor %s", template, vendor_id)
    except Exception as e:
        logger.error("send_vendor_inactivity_email: vendor=%s err=%s", vendor_id, e)


@shared_task(ignore_result=True)
def check_inactive_vendors():
    """
    Daily task that detects inactive vendors and takes graduated action:

    Days since last_seen_at:
      >= warn_days[0] (e.g. 23 days) → first warning email + in-app notification
      >= warn_days[1] (e.g. 27 days) → urgent warning email + in-app notification
      >= inactivity_days (e.g. 30)   → auto-close shop + email + in-app notification

    Vendors that are already suspended, not approved, or have no active
    subscription are skipped — there is nothing meaningful to close.
    """
    from django.utils import timezone as tz
    from datetime import timedelta
    from .models import Vendor, VendorActivityLog
    from notification.models import Notification

    inactivity_days = getattr(settings, 'VENDOR_INACTIVITY_DAYS', 30)
    warn_thresholds = getattr(settings, 'VENDOR_INACTIVITY_WARN_DAYS', [7, 3])
    now = tz.now()

    # Candidates: verified, approved, active-subscription vendors that haven't
    # been manually suspended and aren't already auto-closed.
    candidates = Vendor.objects.filter(
        status='VERIFIED',
        is_approved=True,
        is_suspended=False,
        is_subscribed=True,
        inactivity_auto_closed=False,
    ).select_related('user')

    closed_count = 0
    warned_count = 0

    for vendor in candidates:
        # Use last_seen_at if available; fall back to last_login_at then created_at.
        reference = vendor.last_seen_at or vendor.last_login_at or vendor.created_at
        days_inactive = (now - reference).days

        if days_inactive >= inactivity_days:
            # Auto-close shop
            Vendor.objects.filter(pk=vendor.pk).update(
                inactivity_auto_closed=True,
                inactivity_closed_at=now,
            )
            VendorActivityLog.objects.create(
                vendor=vendor,
                event_type='auto_close',
                metadata={'days_inactive': days_inactive},
            )
            Notification.objects.create(
                recipient=vendor.user,
                verb='vendor_auto_closed',
                data={
                    'message': f'Your shop has been temporarily closed after {days_inactive} days of inactivity. Log in to reopen it.',
                    'days_inactive': days_inactive,
                },
            )
            send_vendor_inactivity_email.delay(
                vendor.pk,
                'email/vendor-auto-closed.html',
                'Your Negromart shop has been temporarily closed',
                {'days_inactive': days_inactive},
            )
            send_vendor_inactivity_sms.delay(
                vendor.pk,
                f"Negromart: Your shop was closed after {days_inactive} days inactive. "
                f"Log in to reopen: https://seller.negromart.com/auth/login",
            )
            closed_count += 1

        elif days_inactive >= inactivity_days - warn_thresholds[1]:
            # Urgent warning (e.g. 27 days → 3 days left)
            days_left = inactivity_days - days_inactive
            Notification.objects.create(
                recipient=vendor.user,
                verb='vendor_inactivity_warning',
                data={
                    'message': f'Your shop closes in {days_left} day(s) due to inactivity. Log in to keep it active.',
                    'days_left': days_left,
                },
            )
            send_vendor_inactivity_email.delay(
                vendor.pk,
                'email/vendor-inactivity-warning.html',
                f'Action required: your shop closes in {days_left} day(s)',
                {'days_inactive': days_inactive, 'days_left': days_left, 'urgent': True},
            )
            send_vendor_inactivity_sms.delay(
                vendor.pk,
                f"Negromart: URGENT - Your shop closes in {days_left} day(s). "
                f"Log in now: https://seller.negromart.com/auth/login",
            )
            warned_count += 1

        elif days_inactive >= inactivity_days - warn_thresholds[0]:
            # First warning (e.g. 23 days → 7 days left)
            days_left = inactivity_days - days_inactive
            Notification.objects.create(
                recipient=vendor.user,
                verb='vendor_inactivity_warning',
                data={
                    'message': f'Your shop closes in {days_left} day(s) if you remain inactive. Log in to keep it active.',
                    'days_left': days_left,
                },
            )
            send_vendor_inactivity_email.delay(
                vendor.pk,
                'email/vendor-inactivity-warning.html',
                f'Heads up: your shop closes in {days_left} day(s)',
                {'days_inactive': days_inactive, 'days_left': days_left, 'urgent': False},
            )
            send_vendor_inactivity_sms.delay(
                vendor.pk,
                f"Negromart: Your shop closes in {days_left} day(s) due to inactivity. "
                f"Log in to keep it open: https://seller.negromart.com/auth/login",
            )
            warned_count += 1

    logger.info(
        "check_inactive_vendors: auto-closed=%d warned=%d threshold=%d days",
        closed_count, warned_count, inactivity_days,
    )
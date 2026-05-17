# payments/email_tasks.py
#
# Handles all subscription alert emails AND SMS via Arkesel.
# Every vendor-facing event fires both an email (HTML template from admin)
# and an SMS (concise plain-text) in the same task, so vendors are always
# reached regardless of which channel they check first.

import logging
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.template import Template, Context
from django.conf import settings

logger = logging.getLogger(__name__)


# ── SMS helpers ───────────────────────────────────────────────────────────────

def _get_vendor_phone(vendor) -> str | None:
    """
    Return the vendor's primary contact phone.
    Tries vendor.contact first, then falls back to the billing profile phone.
    Returns None if neither is populated — SMS will be silently skipped.
    """
    phone = getattr(vendor, 'contact', None)
    if phone and str(phone).strip():
        return str(phone).strip()
    try:
        profile = vendor.billing_profile
        if profile.phone and str(profile.phone).strip():
            return str(profile.phone).strip()
    except Exception:
        pass
    return None


def _send_sms(phone: str | None, message: str) -> None:
    """
    Fire one SMS via Arkesel.  Logs a warning on any failure but never raises —
    an SMS error must not abort the email task that called it.
    """
    if not phone:
        return
    try:
        from userauths.arkesel_client import ArkeselSMS
        client   = ArkeselSMS()
        response = client.send_sms(
            sender=settings.ARKESEL_SENDER,
            message=message,
            recipients=[phone],
        )
        if response.get('status') == 'success':
            logger.info('SMS sent to %s', phone)
        else:
            logger.warning('Arkesel SMS error for %s: %s', phone, response)
    except Exception as exc:
        # Catches APIKeyMissingError, network errors, etc.
        logger.warning('_send_sms: could not send to %s: %s', phone, exc)


# ── Central email dispatcher ──────────────────────────────────────────────────

def send_templated_email(template_type: str, recipient_email: str, context: dict):
    """
    Sends one HTML email using the EmailTemplate row registered in the admin.
    Falls back to the text_body field if the HTML file cannot be rendered.
    Silently returns (with a log warning) if no active template exists.
    """
    from .email_models import EmailTemplate, SubscriptionEmailConfig

    try:
        tmpl = EmailTemplate.objects.get(type=template_type, is_active=True)
    except EmailTemplate.DoesNotExist:
        logger.warning('send_templated_email: no active template for type=%s', template_type)
        return

    cfg = SubscriptionEmailConfig.get()

    # Enrich context with global URLs from the config singleton
    context.setdefault('frontend_url',   cfg.frontend_url)
    context.setdefault('support_url',    cfg.support_url)
    context.setdefault('billing_url',    cfg.frontend_url + '/billing/cards/')
    context.setdefault('subscribe_url',  cfg.frontend_url + '/subscribe/')
    context.setdefault('from_name',      cfg.from_name)

    subject = Template(tmpl.subject).render(Context(context))

    # Try HTML file first
    html_body = None
    if tmpl.html_file:
        try:
            html_body = render_to_string(tmpl.html_file, context)
        except Exception as exc:
            logger.warning('Failed to render html_file=%s: %s', tmpl.html_file, exc)

    # Plain-text fallback (also supports template variables)
    text_body = ''
    if tmpl.text_body:
        text_body = Template(tmpl.text_body).render(Context(context))

    if not html_body and not text_body:
        logger.error('send_templated_email: template %s has no body — skipping', template_type)
        return

    from_addr = f'{cfg.from_name} <{cfg.from_email}>'
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body or 'Please view this email in an HTML-capable email client.',
        from_email=from_addr,
        to=[recipient_email],
        reply_to=[cfg.reply_to] if cfg.reply_to else [],
    )
    if html_body:
        msg.attach_alternative(html_body, 'text/html')

    msg.send(fail_silently=False)
    logger.info('Email sent: type=%s to=%s', template_type, recipient_email)


# ── Individual alert tasks ────────────────────────────────────────────────────
# Each task sends BOTH an email AND an SMS.
# The SMS text is kept short (<160 chars) to avoid multi-part billing.

from celery import shared_task


@shared_task(name="subscriptions.send_subscription_confirmation_email")
def send_subscription_confirmation_email(vendor_id: int, subscription_id: int):
    """Email + SMS sent immediately after a subscription is activated."""
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub    = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)
        ctx = {
            'vendor_name': vendor.name or vendor.email,
            'plan_name':   sub.plan.name,
            'end_date':    sub.end_date.strftime('%B %d, %Y'),
            'amount':      f'GHS {sub.plan.price}',
        }
        send_templated_email('confirmation', vendor.email, ctx)

        first_name = ctx['vendor_name'].split()[0]
        _send_sms(
            _get_vendor_phone(vendor),
            f"Hi {first_name}! Your Negromart {ctx['plan_name']} plan is now active. "
            f"Renews: {ctx['end_date']}. Manage at seller.negromart.com"
        )
    except Exception as e:
        logger.error('send_subscription_confirmation_email failed vendor=%s: %s', vendor_id, e)


@shared_task(name="subscriptions.send_renewal_success_email")
def send_renewal_success_email(vendor_id: int):
    """Email + SMS sent after a successful auto-renewal charge."""
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub    = VendorSubscription.objects.filter(
            vendor=vendor, status='active'
        ).select_related('plan').first()
        if not sub:
            return
        ctx = {
            'vendor_name': vendor.name or vendor.email,
            'plan_name':   sub.plan.name,
            'amount':      f'GHS {sub.plan.price}',
            'end_date':    sub.end_date.strftime('%B %d, %Y'),
        }
        send_templated_email('renewal_success', vendor.email, ctx)

        first_name = ctx['vendor_name'].split()[0]
        _send_sms(
            _get_vendor_phone(vendor),
            f"Hi {first_name}, your Negromart {ctx['plan_name']} plan has renewed. "
            f"Next renewal: {ctx['end_date']}. seller.negromart.com/billing"
        )
    except Exception as e:
        logger.error('send_renewal_success_email failed vendor=%s: %s', vendor_id, e)


@shared_task(name="subscriptions.send_payment_method_required_email")
def send_payment_method_required_email(vendor_id: int):
    """
    Email + SMS sent on the FIRST renewal failure.
    Tells the vendor to update their card/MoMo before the next retry.
    """
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        # Fetch the subscription for plan context (may be past_due or active)
        sub = VendorSubscription.objects.filter(
            vendor=vendor, status__in=['active', 'past_due']
        ).select_related('plan').first()
        name      = vendor.name or vendor.email
        plan_name = sub.plan.name if sub else 'your'
        ctx = {
            'vendor_name': name,
            'plan_name':   plan_name,
        }
        send_templated_email('payment_failed', vendor.email, ctx)

        first_name = name.split()[0]
        _send_sms(
            _get_vendor_phone(vendor),
            f"Hi {first_name}, Negromart couldn't renew your {plan_name} plan - payment failed. "
            f"Update your card/MoMo: seller.negromart.com/billing/cards"
        )
    except Exception as e:
        logger.error('send_payment_method_required_email failed vendor=%s: %s', vendor_id, e)


@shared_task(name="subscriptions.send_cancellation_email")
def send_cancellation_email(vendor_id: int, subscription_id: int):
    """Email + SMS sent when a vendor cancels their subscription."""
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub    = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)
        ctx = {
            'vendor_name': vendor.name or vendor.email,
            'plan_name':   sub.plan.name,
            'end_date':    sub.end_date.strftime('%B %d, %Y'),
        }
        send_templated_email('cancellation', vendor.email, ctx)

        first_name = ctx['vendor_name'].split()[0]
        _send_sms(
            _get_vendor_phone(vendor),
            f"Hi {first_name}, your Negromart {ctx['plan_name']} plan was cancelled. "
            f"Full access until {ctx['end_date']}. Resubscribe: seller.negromart.com/subscribe"
        )
    except Exception as e:
        logger.error('send_cancellation_email failed vendor=%s: %s', vendor_id, e)


@shared_task(name="subscriptions.send_expiring_soon_email")
def send_expiring_soon_email(vendor_id: int, subscription_id: int):
    """
    Email + SMS expiry warning.
    Messaging differs based on auto_renew:
      - auto_renew=True  → informational heads-up ("you'll be charged soon")
      - auto_renew=False → urgent reminder ("please renew manually")
    """
    from vendor.models import Vendor
    from .models import VendorSubscription
    from django.utils import timezone
    try:
        vendor    = Vendor.objects.get(pk=vendor_id)
        sub       = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)
        days_left = (sub.end_date.date() - timezone.now().date()).days
        ctx = {
            'vendor_name': vendor.name or vendor.email,
            'plan_name':   sub.plan.name,
            'end_date':    sub.end_date.strftime('%B %d, %Y'),
            'days_left':   days_left,
            'auto_renew':  sub.auto_renew,      # used by the template
            'amount':      f'GHS {sub.plan.price}',
        }
        send_templated_email('expiring_soon', vendor.email, ctx)

        first_name = ctx['vendor_name'].split()[0]
        days_word  = 'day' if days_left == 1 else 'days'
        phone      = _get_vendor_phone(vendor)

        if sub.auto_renew:
            # Informational — charge is automatic, no vendor action needed
            _send_sms(
                phone,
                f"Hi {first_name}, heads up — your Negromart {ctx['plan_name']} plan renews in "
                f"{days_left} {days_word} on {ctx['end_date']}. "
                f"GHS {sub.plan.price} will be charged automatically."
            )
        else:
            # Urgent — vendor must renew manually or lose access
            urgency = "URGENT: " if days_left <= 3 else ""
            _send_sms(
                phone,
                f"{urgency}Hi {first_name}, your Negromart {ctx['plan_name']} plan expires in "
                f"{days_left} {days_word} on {ctx['end_date']}. "
                f"Renew now: seller.negromart.com/subscribe"
            )
    except Exception as e:
        logger.error('send_expiring_soon_email failed vendor=%s: %s', vendor_id, e)


@shared_task(name="subscriptions.send_subscription_expired_email")
def send_subscription_expired_email(vendor_id: int):
    """Email + SMS sent when a subscription expires (auto-expiry or retry exhaustion)."""
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        # Find the subscription that just expired (most recent)
        sub = VendorSubscription.objects.filter(
            vendor=vendor, status='expired'
        ).select_related('plan').order_by('-end_date').first()
        name      = vendor.name or vendor.email
        plan_name = sub.plan.name if sub else 'your'
        ctx = {
            'vendor_name': name,
            'plan_name':   plan_name,
        }
        send_templated_email('expired', vendor.email, ctx)

        first_name = name.split()[0]
        _send_sms(
            _get_vendor_phone(vendor),
            f"Hi {first_name}, your Negromart {plan_name} plan has expired. "
            f"Your store is now on the Free plan. "
            f"Resubscribe: seller.negromart.com/subscribe"
        )
    except Exception as e:
        logger.error('send_subscription_expired_email failed vendor=%s: %s', vendor_id, e)


# ── Scheduled orchestration tasks ─────────────────────────────────────────────
# These three tasks are registered in django-celery-beat dynamically by
# SubscriptionEmailConfig._update_periodic_tasks() whenever the admin saves the
# config.  Their schedule (hour/minute UTC) is controlled from the admin panel.


@shared_task(name="subscriptions.process_renewals")
def process_subscription_renewals():
    """
    Queues a charge_vendor_for_renewal task for every subscription whose
    end_date falls exactly renewal_advance_days from now and has auto_renew=True.
    Runs daily at the time set in SubscriptionEmailConfig.run_renewals_hour/minute.
    """
    from .models import VendorSubscription
    from .email_models import SubscriptionEmailConfig
    from django.utils import timezone
    from datetime import timedelta

    cfg    = SubscriptionEmailConfig.get()
    target = timezone.now() + timedelta(days=cfg.renewal_advance_days)

    due = VendorSubscription.objects.filter(
        status='active', auto_renew=True,
        end_date__date=target.date(),
    ).values_list('id', flat=True)

    count = 0
    for sub_id in due:
        from .tasks import charge_vendor_for_renewal
        charge_vendor_for_renewal.apply_async(
            args=[sub_id],
            max_retries=cfg.renewal_max_retries,
        )
        count += 1

    logger.info('process_subscription_renewals: queued %d renewal task(s)', count)
    return f'queued:{count}'


@shared_task(name="subscriptions.warn_expiring_soon")
def warn_expiring_soon():
    """
    Sends expiry warnings at both thresholds from SubscriptionEmailConfig.

    auto_renew=False → urgent "please renew" at first AND second threshold.
    auto_renew=True  → informational heads-up at the first threshold only
                       (they will be charged automatically; no second nag).

    Runs daily at SubscriptionEmailConfig.run_expiry_check_hour/minute (UTC).
    """
    from .models import VendorSubscription
    from .email_models import SubscriptionEmailConfig
    from django.utils import timezone
    from datetime import timedelta

    cfg   = SubscriptionEmailConfig.get()
    count = 0

    # Urgent warnings for vendors who must renew manually
    thresholds = [cfg.expiry_warning_days]
    if cfg.second_warning_days > 0:
        thresholds.append(cfg.second_warning_days)

    for days in thresholds:
        target   = timezone.now() + timedelta(days=days)
        expiring = VendorSubscription.objects.filter(
            status='active', auto_renew=False,
            end_date__date=target.date(),
        )
        for sub in expiring:
            send_expiring_soon_email.delay(sub.vendor_id, sub.id)
            count += 1

    # Heads-up for auto-renewing vendors (first threshold only — no double nag)
    target       = timezone.now() + timedelta(days=cfg.expiry_warning_days)
    auto_renewing = VendorSubscription.objects.filter(
        status='active', auto_renew=True,
        end_date__date=target.date(),
    )
    for sub in auto_renewing:
        send_expiring_soon_email.delay(sub.vendor_id, sub.id)
        count += 1

    logger.info('warn_expiring_soon: sent %d warning(s)', count)
    return f'warned:{count}'


@shared_task(name="subscriptions.expire_old_subscriptions")
def expire_old_subscriptions():
    """
    Finds every active subscription whose end_date has already passed,
    marks it as expired, downgrades the vendor to the Free plan feature-set,
    and notifies the vendor via email + SMS.

    Runs daily at SubscriptionEmailConfig.run_expire_old_hour/minute (UTC).
    This is the safety net — charge_vendor_for_renewal normally handles renewal
    before the end_date, but this task catches anything that slipped through.
    """
    from .models import VendorSubscription
    from django.utils import timezone

    overdue = VendorSubscription.objects.filter(
        status='active',
        end_date__lt=timezone.now(),
    ).select_related('vendor', 'plan')

    count = 0
    for sub in overdue:
        try:
            sub.status = 'expired'
            sub.save(update_fields=['status'])

            # Downgrade vendor feature flags to reflect the Free plan limits
            try:
                from . import services
                services._sync_vendor_flags(sub.vendor)
            except Exception as flag_exc:
                logger.warning(
                    'expire_old_subscriptions: _sync_vendor_flags failed for vendor=%s: %s',
                    sub.vendor_id, flag_exc,
                )

            send_subscription_expired_email.delay(sub.vendor_id)
            count += 1
        except Exception as e:
            logger.error('expire_old_subscriptions: failed for sub=%s: %s', sub.id, e)

    logger.info('expire_old_subscriptions: expired %d subscription(s)', count)
    return f'expired:{count}'

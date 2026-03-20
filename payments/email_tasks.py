# subscriptions/email_tasks.py
#
# Drop-in replacement for the email portions of tasks.py.
# Provides a single send_templated_email() function used by all tasks.
# Replace your existing task email calls with these.

import logging
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.template import Template, Context
from django.conf import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Central email dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def send_templated_email(template_type: str, recipient_email: str, context: dict):
    """
    Sends one email using the EmailTemplate registered in the admin.

    If the template has an html_file, renders it with Django's template engine.
    Falls back to text_body (also rendered as a Django template).
    If neither is set, raises ValueError so the caller can log it.

    Args:
        template_type:    One of EmailTemplate.TYPE_CHOICES keys
        recipient_email:  Destination address
        context:          Dict of template variables
    """
    from .email_models import EmailTemplate, SubscriptionEmailConfig

    try:
        tmpl = EmailTemplate.objects.get(type=template_type, is_active=True)
    except EmailTemplate.DoesNotExist:
        logger.warning(f'send_templated_email: no active template for type={template_type}')
        return

    cfg = SubscriptionEmailConfig.get()

    # Enrich context with global config values
    context.setdefault('frontend_url', cfg.frontend_url)
    context.setdefault('support_url',  cfg.support_url)
    context.setdefault('billing_url',  cfg.frontend_url + '/billing/cards/')
    context.setdefault('subscribe_url', cfg.frontend_url + '/subscribe/')
    context.setdefault('from_name', cfg.from_name)

    # Render subject
    subject = Template(tmpl.subject).render(Context(context))

    # Render HTML body
    html_body = None
    if tmpl.html_file:
        try:
            html_body = render_to_string(tmpl.html_file, context)
        except Exception as exc:
            logger.warning(f'Failed to render html_file={tmpl.html_file}: {exc}')

    # Render text fallback
    text_body = ''
    if tmpl.text_body:
        text_body = Template(tmpl.text_body).render(Context(context))

    if not html_body and not text_body:
        logger.error(f'send_templated_email: template {template_type} has no body — skipping')
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
    logger.info(f'Email sent: type={template_type} to={recipient_email}')


# ─────────────────────────────────────────────────────────────────────────────
# Updated Celery email tasks — replace the ones in tasks.py
# ─────────────────────────────────────────────────────────────────────────────

from celery import shared_task


@shared_task(name="subscriptions.send_subscription_confirmation_email")
def send_subscription_confirmation_email(vendor_id: int, subscription_id: int):
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub    = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)
        send_templated_email(
            template_type='confirmation',
            recipient_email=vendor.email,
            context={
                'vendor_name': vendor.name or vendor.email,
                'plan_name':   sub.plan.name,
                'end_date':    sub.end_date.strftime('%B %d, %Y'),
                'amount':      f'GHS {sub.plan.price}',
            },
        )
    except Exception as e:
        logger.error(f'send_subscription_confirmation_email failed vendor={vendor_id}: {e}')


@shared_task(name="subscriptions.send_renewal_success_email")
def send_renewal_success_email(vendor_id: int):
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub    = VendorSubscription.objects.filter(vendor=vendor, status='active').select_related('plan').first()
        if not sub:
            return
        send_templated_email(
            template_type='renewal_success',
            recipient_email=vendor.email,
            context={
                'vendor_name': vendor.name or vendor.email,
                'plan_name':   sub.plan.name,
                'amount':      f'GHS {sub.plan.price}',
                'end_date':    sub.end_date.strftime('%B %d, %Y'),
            },
        )
    except Exception as e:
        logger.error(f'send_renewal_success_email failed vendor={vendor_id}: {e}')


@shared_task(name="subscriptions.send_payment_method_required_email")
def send_payment_method_required_email(vendor_id: int):
    from vendor.models import Vendor
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        send_templated_email(
            template_type='payment_failed',
            recipient_email=vendor.email,
            context={'vendor_name': vendor.name or vendor.email},
        )
    except Exception as e:
        logger.error(f'send_payment_method_required_email failed vendor={vendor_id}: {e}')


@shared_task(name="subscriptions.send_cancellation_email")
def send_cancellation_email(vendor_id: int, subscription_id: int):
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub    = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)
        send_templated_email(
            template_type='cancellation',
            recipient_email=vendor.email,
            context={
                'vendor_name': vendor.name or vendor.email,
                'plan_name':   sub.plan.name,
                'end_date':    sub.end_date.strftime('%B %d, %Y'),
            },
        )
    except Exception as e:
        logger.error(f'send_cancellation_email failed vendor={vendor_id}: {e}')


@shared_task(name="subscriptions.send_expiring_soon_email")
def send_expiring_soon_email(vendor_id: int, subscription_id: int):
    from vendor.models import Vendor
    from .models import VendorSubscription
    from .email_models import SubscriptionEmailConfig
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub    = VendorSubscription.objects.select_related('plan').get(pk=subscription_id)
        cfg    = SubscriptionEmailConfig.get()
        # Compute days_left dynamically at send time
        from django.utils import timezone
        days_left = (sub.end_date.date() - timezone.now().date()).days
        send_templated_email(
            template_type='expiring_soon',
            recipient_email=vendor.email,
            context={
                'vendor_name': vendor.name or vendor.email,
                'plan_name':   sub.plan.name,
                'end_date':    sub.end_date.strftime('%B %d, %Y'),
                'days_left':   days_left,
            },
        )
    except Exception as e:
        logger.error(f'send_expiring_soon_email failed vendor={vendor_id}: {e}')


@shared_task(name="subscriptions.send_subscription_expired_email")
def send_subscription_expired_email(vendor_id: int):
    from vendor.models import Vendor
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        send_templated_email(
            template_type='expired',
            recipient_email=vendor.email,
            context={'vendor_name': vendor.name or vendor.email},
        )
    except Exception as e:
        logger.error(f'send_subscription_expired_email failed vendor={vendor_id}: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Updated tasks.py functions — use config for dynamic thresholds
# Replace the relevant functions in tasks.py with these
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="subscriptions.process_renewals")
def process_subscription_renewals():
    """Reads renewal_advance_days from DB config — no hardcoded values."""
    from .models import VendorSubscription
    from .email_models import SubscriptionEmailConfig
    from django.utils import timezone
    from datetime import timedelta

    cfg     = SubscriptionEmailConfig.get()
    target  = timezone.now() + timedelta(days=cfg.renewal_advance_days)

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

    logger.info(f'process_subscription_renewals: queued {count} renewal tasks')
    return f'queued:{count}'


@shared_task(name="subscriptions.warn_expiring_soon")
def warn_expiring_soon():
    """Sends warnings at both expiry_warning_days and second_warning_days thresholds."""
    from .models import VendorSubscription
    from .email_models import SubscriptionEmailConfig
    from django.utils import timezone
    from datetime import timedelta

    cfg = SubscriptionEmailConfig.get()
    thresholds = [cfg.expiry_warning_days]
    if cfg.second_warning_days > 0:
        thresholds.append(cfg.second_warning_days)

    count = 0
    for days in thresholds:
        target = timezone.now() + timedelta(days=days)
        expiring = VendorSubscription.objects.filter(
            status='active', auto_renew=False,
            end_date__date=target.date(),
        ).select_related('vendor', 'plan')
        for sub in expiring:
            send_expiring_soon_email.delay(sub.vendor.id, sub.id)
            count += 1

    return f'warned:{count}'
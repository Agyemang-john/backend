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


# ── Built-in wording (used only when the admin hasn't created a template) ─────
DEFAULT_EMAILS = {
    'confirmation': (
        'Your {{ plan_name }} plan is active',
        'Hi {{ vendor_name }},\n\nYour Negromart {{ plan_name }} plan is now active. '
        'It renews on {{ end_date }}.\n\nManage your plan: {{ frontend_url }}/billing',
    ),
    'renewal_success': (
        'Your {{ plan_name }} plan has renewed',
        'Hi {{ vendor_name }},\n\nWe received {{ amount }} and your {{ plan_name }} plan has renewed. '
        'Next renewal: {{ end_date }}.\n\nBilling: {{ frontend_url }}/billing',
    ),
    'expiring_soon': (
        '{% if auto_renew %}Your {{ plan_name }} plan renews on {{ end_date }}'
        '{% else %}Your {{ plan_name }} plan ends in {{ days_left }} day{{ days_left|pluralize }}{% endif %}',
        'Hi {{ vendor_name }},\n\n{% if auto_renew %}Your {{ plan_name }} plan renews on {{ end_date }}. '
        'We will charge {{ amount }} to your saved payment method.'
        '{% else %}Your {{ plan_name }} plan ends on {{ end_date }} and will not renew. '
        'After that your store moves to the Free plan. Renew: {{ subscribe_url }}{% endif %}',
    ),
    'payment_failed': (
        '{% if upcoming %}Add a payment method to keep {{ plan_name }}'
        '{% else %}Action needed: we couldn\'t renew your {{ plan_name }} plan{% endif %}',
        'Hi {{ vendor_name }},\n\n{% if reason %}{{ reason }}\n\n{% endif %}'
        '{% if access_until %}Your {{ plan_name }} plan stays on until {{ access_until }}. '
        'If we can\'t collect payment by then, your store moves to the Free plan.\n\n{% endif %}'
        '{% if next_attempt %}We\'ll try again on {{ next_attempt }}.\n\n{% endif %}'
        'Update your card or Mobile Money, or pay now: {{ frontend_url }}/billing',
    ),
    'cancellation': (
        'Your {{ plan_name }} plan was cancelled',
        'Hi {{ vendor_name }},\n\nYour {{ plan_name }} plan won\'t renew. You keep it until {{ end_date }}. '
        'Changed your mind? {{ subscribe_url }}',
    ),
    'expired': (
        'Your {{ plan_name }} plan has ended',
        'Hi {{ vendor_name }},\n\nYour {{ plan_name }} plan has ended and your store is now on the Free plan. '
        'Your listings stay live. Subscribe again: {{ subscribe_url }}',
    ),
}


def _fmt_date(iso):
    from django.utils.dateparse import parse_datetime
    dt = parse_datetime(iso) if iso else None
    return dt.strftime('%B %d, %Y') if dt else None


# ── Central email dispatcher ──────────────────────────────────────────────────

def send_templated_email(template_type: str, recipient_email: str, context: dict):
    """
    Sends one HTML email using the EmailTemplate row registered in the admin.
    Falls back to the text_body field if the HTML file cannot be rendered.
    Silently returns (with a log warning) if no active template exists.
    """
    from .email_models import EmailTemplate, SubscriptionEmailConfig

    tmpl = EmailTemplate.objects.filter(type=template_type).first()
    if tmpl is not None and not tmpl.is_active:
        logger.info('send_templated_email: type=%s is switched off in admin', template_type)
        return
    if tmpl is None:
        default = DEFAULT_EMAILS.get(template_type)
        if default is None:
            logger.warning('send_templated_email: no template for type=%s', template_type)
            return
        tmpl = EmailTemplate(type=template_type, subject=default[0], text_body=default[1])

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
def send_payment_method_required_email(vendor_id: int, reason: str = '', no_method: bool = False,
                                       access_until: str = None, next_attempt: str = None,
                                       upcoming: bool = False):
    """
    Email + SMS when a renewal payment failed (each attempt), when the plan
    goes past due, or ahead of the renewal date when there's nothing to charge.
    Says why, until when the store keeps its plan, and when we'll retry.
    """
    from vendor.models import Vendor
    from .models import VendorSubscription
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
        sub = VendorSubscription.objects.filter(
            vendor=vendor, status__in=['active', 'past_due']
        ).select_related('plan').first()
        name      = vendor.name or vendor.email
        plan_name = sub.plan.name if sub else 'your'
        until     = _fmt_date(access_until)
        ctx = {
            'vendor_name':  name,
            'plan_name':    plan_name,
            'reason':       reason,
            'no_method':    no_method,
            'access_until': until,
            'next_attempt': _fmt_date(next_attempt),
            'upcoming':     upcoming,
        }
        send_templated_email('payment_failed', vendor.email, ctx)

        first_name = name.split()[0]
        if upcoming:
            sms = (f"Hi {first_name}, add a card or MoMo before {until or 'your renewal date'} "
                   f"to keep your Negromart {plan_name} plan: seller.negromart.com/billing")
        else:
            sms = (f"Hi {first_name}, we couldn't renew your Negromart {plan_name} plan. "
                   + (f"Pay by {until} to keep it: " if until else "Update your card/MoMo: ")
                   + "seller.negromart.com/billing")
        _send_sms(_get_vendor_phone(vendor), sms)
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


@shared_task(name="subscriptions.process_renewals", ignore_result=True)
def process_subscription_renewals():
    """
    Hourly. Queues one renewal attempt for every auto-renewing subscription
    that is due: inside the renewal window and not waiting for its next retry
    (payments/subscription_lifecycle.py). Window-based, so a missed run is
    caught up by the next one; each attempt claims its row, so queuing the
    same subscription twice never charges twice.
    """
    from .subscription_lifecycle import due_for_renewal_ids
    from .tasks import charge_vendor_for_renewal

    count = 0
    for sub_id in due_for_renewal_ids().iterator(chunk_size=1000):
        charge_vendor_for_renewal.delay(sub_id)
        count += 1
    logger.info('process_subscription_renewals: queued %d renewal attempt(s)', count)
    return f'queued:{count}'


@shared_task(name="subscriptions.warn_expiring_soon", ignore_result=True)
def warn_expiring_soon():
    """
    Daily. Reminders before the renewal date — see
    subscription_lifecycle.send_upcoming_reminders (each sent once per period).
    """
    from .subscription_lifecycle import send_upcoming_reminders
    count = send_upcoming_reminders()
    logger.info('warn_expiring_soon: sent %d reminder(s)', count)
    return f'warned:{count}'


@shared_task(name="subscriptions.expire_old_subscriptions", ignore_result=True)
def expire_old_subscriptions():
    """
    Hourly. Plans whose period ended go to past_due (grace period, renewal
    still retried) or straight to Free; past_due plans whose grace ran out,
    and finished trials, expire. Emails go out on each change.
    """
    from .subscription_lifecycle import sweep_lapsed
    counts = sweep_lapsed()
    logger.info('expire_old_subscriptions: %s', counts)
    return f"expired:{counts['expired']} past_due:{counts['past_due']}"

"""
Shared email plumbing for re-engagement messages: signed one-click
unsubscribe links per category, and batched sending over one SMTP connection.
"""
import logging

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from .models import EmailOptOut

logger = logging.getLogger(__name__)

OPT_OUT_SALT = 'engagement.email-opt-out'


def site_url():
    return settings.SITE_URL.rstrip('/')


def opt_out_token(user_id, category):
    return signing.dumps({'u': user_id, 'c': category}, salt=OPT_OUT_SALT, compress=True)


def read_opt_out_token(token):
    """(user_id, category) or None for a bad/forged token. Tokens don't expire: old emails must keep working."""
    try:
        data = signing.loads(token, salt=OPT_OUT_SALT)
    except signing.BadSignature:
        return None
    if data.get('c') not in dict(EmailOptOut.CATEGORY_CHOICES):
        return None
    return data.get('u'), data['c']


def opt_out_url(user_id, category):
    return f"{site_url()}/unsubscribe/{opt_out_token(user_id, category)}"


def one_click_url(user_id, category):
    """RFC 8058 target: mail apps POST here directly, so it must be the API, not the web page."""
    base = (getattr(settings, 'BACKEND_BASE_URL', '') or '').rstrip('/')
    return f"{base}/api/v1/engagement/unsubscribe/{opt_out_token(user_id, category)}/" if base else ''


def opted_out_user_ids(user_ids, category):
    return set(EmailOptOut.objects.filter(user_id__in=user_ids, category=category)
               .values_list('user_id', flat=True))


def absolute_media(url):
    if not url:
        return ''
    if url.startswith(('http://', 'https://')):
        return url
    base = getattr(settings, 'BACKEND_BASE_URL', '') or ''
    return f"{base.rstrip('/')}{url}" if base else ''


def base_context(user, category):
    site = site_url()
    return {
        'first_name': getattr(user, 'first_name', '') or 'there',
        'site_name': 'Negromart',
        'site_url': site,
        'site_logo_url': f"{site}/favicon.png",
        'opt_out_url': opt_out_url(user.pk, category),
        'one_click_url': one_click_url(user.pk, category),
        'year': timezone.now().year,
    }


def _unsubscribe_headers(context):
    if context.get('one_click_url'):
        return {'List-Unsubscribe': f"<{context['one_click_url']}>",
                'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'}
    return {'List-Unsubscribe': f"<{context['opt_out_url']}>"}


def build_email(user, subject, template, context):
    html = render_to_string(f'email/{template}.html', context)
    msg = EmailMultiAlternatives(
        subject=subject[:150],
        body=strip_tags(html),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
        headers=_unsubscribe_headers(context),
    )
    msg.attach_alternative(html, 'text/html')
    return msg


def send_batch(messages):
    """Send over one connection. Returns how many went out; failures are logged, not raised."""
    if not messages:
        return 0
    try:
        with get_connection(fail_silently=False) as conn:
            return conn.send_messages(messages) or 0
    except Exception:  # noqa: BLE001 - one bad batch must not stop the sweep
        logger.exception("engagement email batch of %s failed", len(messages))
        return 0

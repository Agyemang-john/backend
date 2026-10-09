"""
product/review_reminders.py
"How was your purchase?" reminders: some days after a product is delivered,
ask the customer to rate and review it, by email (plus an in-app notification
and, if switched on, one SMS).

Flow (daily Celery beat → product.tasks.send_review_reminders):
    queue_due_reminders()  finds who is due, writes a QUEUED ReviewReminder row
                           per (customer, product, stage) and hands each
                           customer's batch to a worker.
    deliver(ids)           re-checks every item, then sends one message per
                           customer covering up to MAX_ITEMS_PER_MESSAGE products.

Who is reminded, and when (settings.REVIEW_REMINDERS):
    - the order line was delivered FIRST_AFTER_DAYS ago or more, but not more
      than MAX_AGE_DAYS ago (old purchases are never dug up);
    - it isn't cancelled and has no return in progress or refunded;
    - the product is still published and the customer hasn't reviewed it;
    - the account is active and hasn't opted out.
    A follow-up goes FOLLOW_UP_AFTER_DAYS after delivery if there's still no
    review. That's the last one: at most two reminders per product, ever.
    A customer gets at most one reminder message every MIN_DAYS_BETWEEN days,
    however many orders arrive; products waiting their turn go out next time.

The database decides who is due; nothing here trusts client input. Links point
at the product page (?review=1 opens the review form, &rating=N preselects the
stars) — the page still runs the normal eligibility check before accepting.
"""

import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import ProductReview, ReviewReminder, ReviewReminderOptOut

logger = logging.getLogger('reviews')

DEFAULTS = {
    'ENABLED': True,
    'FIRST_AFTER_DAYS': 7,
    'FOLLOW_UP_AFTER_DAYS': 21,
    'MAX_AGE_DAYS': 45,
    'MIN_DAYS_BETWEEN': 4,
    'MAX_ITEMS_PER_MESSAGE': 4,
    'SMS_ENABLED': False,
}

OPT_OUT_SALT = 'product.review-reminders.opt-out'
RETURN_BLOCKING = ('requested', 'approved', 'received', 'refunded')


def conf(key):
    return {**DEFAULTS, **getattr(settings, 'REVIEW_REMINDERS', {})}[key]


# ── Opt-out ──────────────────────────────────────────────────────────────────

def opt_out_token(user):
    """Signed, non-expiring token for the one-click link in the email."""
    return signing.dumps(user.pk, salt=OPT_OUT_SALT)


def user_from_opt_out_token(token):
    from django.contrib.auth import get_user_model
    try:
        pk = signing.loads(token, salt=OPT_OUT_SALT)
    except signing.BadSignature:
        return None
    return get_user_model().objects.filter(pk=pk).first()


def is_opted_out(user):
    return ReviewReminderOptOut.objects.filter(user=user).exists()


def set_opted_out(user, opted_out=True):
    if opted_out:
        ReviewReminderOptOut.objects.get_or_create(user=user)
    else:
        ReviewReminderOptOut.objects.filter(user=user).delete()
    logger.info("review reminders %s for user %s", 'off' if opted_out else 'on', user.pk)


def mark_reviewed(user, product):
    """Called when a review is written, to measure how many reminders convert."""
    ReviewReminder.objects.filter(user=user, product=product, reviewed_at__isnull=True,
                                  status=ReviewReminder.SENT).update(reviewed_at=timezone.now())


# ── Finding who is due ───────────────────────────────────────────────────────

def _candidate_lines(now):
    """Delivered, still-reviewable order lines in the reminder window, newest first."""
    from order.models import OrderProduct, ReturnRequest

    newest = now - timedelta(days=conf('FIRST_AFTER_DAYS'))
    oldest = now - timedelta(days=conf('MAX_AGE_DAYS'))
    reviewed = ProductReview.objects.filter(user=OuterRef('order__user'), product=OuterRef('product'))
    opted_out = ReviewReminderOptOut.objects.filter(user=OuterRef('order__user'))
    returned = ReturnRequest.objects.filter(order_product=OuterRef('pk'), status__in=RETURN_BLOCKING)

    return (
        OrderProduct.objects
        # Older lines were marked delivered on the order only; the order's last
        # update is the closest thing they have to a delivery date.
        .annotate(delivered_on=Coalesce('delivered_date', 'order__date_updated'))
        .filter(Q(delivered_date__isnull=False) | Q(order__status='delivered'))
        .filter(order__is_ordered=True, order__user__isnull=False, order__user__is_active=True,
                product__status='published', delivered_on__gte=oldest, delivered_on__lte=newest)
        .exclude(status='canceled')
        .exclude(order__status='canceled')
        .exclude(Exists(reviewed))
        .exclude(Exists(opted_out))
        .exclude(Exists(returned))
        .order_by('order__user_id', '-delivered_on')
        .values_list('pk', 'order__user_id', 'product_id', 'delivered_on')
    )


def due_batches(now=None):
    """
    {user_id: [(order_line_id, product_id, stage), ...]} for customers who
    should get a reminder message now. Read-only.
    """
    now = now or timezone.now()
    follow_up_days = conf('FOLLOW_UP_AFTER_DAYS')
    gap = timedelta(days=conf('MIN_DAYS_BETWEEN'))
    limit = conf('MAX_ITEMS_PER_MESSAGE')

    # Latest delivered line per (customer, product): one review per product.
    latest = {}
    for line_id, user_id, product_id, delivered_on in _candidate_lines(now).iterator(chunk_size=2000):
        latest.setdefault((user_id, product_id), (line_id, delivered_on))
    if not latest:
        return {}

    user_ids = {u for u, _ in latest}
    # Customers messaged recently wait (queued and failed attempts count too).
    resting = set(ReviewReminder.objects.filter(user_id__in=user_ids, created_at__gt=now - gap)
                  .values_list('user_id', flat=True))
    previous = defaultdict(dict)   # (user, product) -> {stage: created_at}
    for user_id, product_id, stage, created_at in (
            ReviewReminder.objects.filter(user_id__in=user_ids - resting)
            .values_list('user_id', 'product_id', 'stage', 'created_at')):
        previous[(user_id, product_id)][stage] = created_at

    batches = defaultdict(list)
    for (user_id, product_id), (line_id, delivered_on) in latest.items():
        if user_id in resting or len(batches[user_id]) >= limit:
            continue
        sent = previous.get((user_id, product_id), {})
        if not sent:
            stage = ReviewReminder.FIRST
        elif (follow_up_days and ReviewReminder.FIRST in sent and ReviewReminder.FOLLOW_UP not in sent
              and delivered_on <= now - timedelta(days=follow_up_days)):
            stage = ReviewReminder.FOLLOW_UP
        else:
            continue
        batches[user_id].append((line_id, product_id, stage))
    return {u: items for u, items in batches.items() if items}


def queue_due_reminders(now=None):
    """Write QUEUED rows for everyone due and dispatch one send task per customer."""
    from .tasks import send_review_reminder

    if not conf('ENABLED'):
        logger.info("review reminders disabled; nothing queued")
        return 0
    queued_customers = 0
    for user_id, items in due_batches(now).items():
        ids = []
        for line_id, product_id, stage in items:
            try:
                with transaction.atomic():
                    ids.append(ReviewReminder.objects.create(
                        user_id=user_id, product_id=product_id, order_item_id=line_id, stage=stage).pk)
            except IntegrityError:
                pass  # another run queued this one already
        if ids:
            transaction.on_commit(lambda ids=ids: send_review_reminder.delay(ids))
            queued_customers += 1
    logger.info("review reminders: queued messages for %s customer(s)", queued_customers)
    return queued_customers


# ── Sending ──────────────────────────────────────────────────────────────────

def _still_due(reminder):
    product = reminder.product
    return (reminder.user.is_active and product.status == 'published'
            and not ProductReview.objects.filter(user=reminder.user, product=product).exists()
            and not is_opted_out(reminder.user))


def _absolute(url):
    if not url:
        return ''
    if url.startswith(('http://', 'https://')):
        return url
    base = getattr(settings, 'BACKEND_BASE_URL', '') or ''
    return f"{base.rstrip('/')}{url}" if base else ''


def product_url(product, *, rating=None, channel='email'):
    url = f"{settings.SITE_URL.rstrip('/')}/{product.sku}/{product.slug}?review=1"
    if rating:
        url += f"&rating={rating}"
    return f"{url}&utm_source=review_reminder&utm_medium={channel}"


def _item_context(reminder):
    from .review_services import describe_variant
    product, line = reminder.product, reminder.order_item
    delivered = (line.delivered_date or line.order.date_updated) if line else None
    try:
        image = product.image.url if product.image else ''
    except ValueError:
        image = ''
    return {
        'title': product.title,
        'variant': describe_variant(line),
        'seller': getattr(product.vendor, 'name', '') or '',
        'image_url': _absolute(image),
        'delivered_on': delivered,
        'order_url': f"{settings.SITE_URL.rstrip('/')}/dashboard/order-history/{line.order_id}" if line else '',
        'review_url': product_url(product),
        'rating_urls': [(n, product_url(product, rating=n)) for n in (5, 4, 3, 2, 1)],
    }


def deliver(reminder_ids):
    """
    Send one message covering these QUEUED reminders (all for one customer).
    Raises on an email failure so the task can retry; SMS and in-app
    notification problems are logged and don't block the email.
    Returns the channels used, or [] if nothing was sent.
    """
    reminders = list(
        ReviewReminder.objects.filter(pk__in=reminder_ids, status=ReviewReminder.QUEUED)
        .select_related('user', 'product__vendor', 'order_item__order',
                        'order_item__variant__size', 'order_item__variant__color')
        .order_by('stage', '-created_at')
    )
    live = []
    for r in reminders:
        if _still_due(r):
            live.append(r)
        else:
            r.status = ReviewReminder.SKIPPED
            r.save(update_fields=['status'])
    if not live:
        return []

    user = live[0].user
    items = [_item_context(r) for r in live]
    follow_up = all(r.stage == ReviewReminder.FOLLOW_UP for r in live)
    channels = []

    if user.email:
        _send_email(user, items, follow_up)
        channels.append('email')
    if conf('SMS_ENABLED') and user.phone and user.phone_verified_at and not follow_up:
        if _send_sms(user, live[0].product, more=len(live) - 1):
            channels.append('sms')
    if _send_in_app(user, live):
        channels.append('in_app')

    now = timezone.now()
    for r in live:
        r.status, r.sent_at, r.channels, r.error = ReviewReminder.SENT, now, channels, ''
        r.save(update_fields=['status', 'sent_at', 'channels', 'error'])
    logger.info("review reminder sent to user %s: %s product(s) via %s", user.pk, len(live), channels)
    return channels


def mark_failed(reminder_ids, error):
    ReviewReminder.objects.filter(pk__in=reminder_ids, status=ReviewReminder.QUEUED).update(
        status=ReviewReminder.FAILED, error=str(error)[:255])


def _send_email(user, items, follow_up):
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.utils.html import strip_tags

    site = settings.SITE_URL.rstrip('/')
    opt_out_url = f"{site}/reviews/reminders/unsubscribe/{opt_out_token(user)}"
    first = items[0]['title']
    if len(items) == 1:
        subject = (f"Still have a minute? Tell others about your {first}" if follow_up
                   else f"How's your {first}? Share your experience")
    else:
        subject = "How are your recent purchases? Share your experience"
    context = {
        'first_name': user.first_name or 'there',
        'items': items,
        'follow_up': follow_up,
        'reviews_url': f"{site}/dashboard/reviews",
        'opt_out_url': opt_out_url,
        'site_name': 'Negromart',
        'site_url': site,
        'site_logo_url': f"{site}/favicon.png",
        'year': timezone.now().year,
    }
    html = render_to_string('email/review_reminder.html', context)
    email = EmailMultiAlternatives(
        subject=subject[:150],
        body=render_to_string('email/review_reminder.txt', context) or strip_tags(html),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
        headers={'List-Unsubscribe': f"<{opt_out_url}>"},
    )
    email.attach_alternative(html, 'text/html')
    email.send(fail_silently=False)


def _send_sms(user, product, more=0):
    from userauths.arkesel_client import ArkeselSMS
    name = product.title if len(product.title) <= 40 else product.title[:37] + '...'
    extra = f" and {more} other item{'s' if more > 1 else ''}" if more else ''
    message = (f"Hi {user.first_name or 'there'}, how is your {name}{extra}? "
               f"Rate it on Negromart: {product_url(product, channel='sms')}")
    try:
        response = ArkeselSMS().send_sms(sender=settings.ARKESEL_SENDER, message=message,
                                         recipients=[user.phone])
    except Exception:  # noqa: BLE001 - SMS is best effort
        logger.exception("review reminder SMS to user %s failed", user.pk)
        return False
    if (response or {}).get('status') != 'success':
        logger.warning("review reminder SMS to user %s rejected: %s", user.pk, response)
        return False
    return True


def _send_in_app(user, reminders):
    from notification.utils import send_notification
    product = reminders[0].product
    if len(reminders) == 1:
        message, url = f"How's your {product.title}? Rate it to help other shoppers.", \
            f"/{product.sku}/{product.slug}?review=1"
    else:
        message, url = (f"Rate your {len(reminders)} recent purchases to help other shoppers.",
                        "/dashboard/reviews")
    try:
        send_notification(recipient=user, verb='customer_review_reminder', target=product,
                          data={'message': message, 'url': url, 'product_id': product.pk})
    except Exception:  # noqa: BLE001 - notification is best effort
        logger.exception("review reminder notification for user %s failed", user.pk)
        return False
    return True


# ── "Waiting for your review" list (customer dashboard) ─────────────────────

def awaiting_review(user, limit=20):
    """Products this customer received and can still review, newest delivery first."""
    from order.models import OrderProduct
    reviewed = ProductReview.objects.filter(user=user, product=OuterRef('product'))
    lines = (
        OrderProduct.objects
        .filter(order__user=user, order__is_ordered=True, product__status='published')
        .filter(Q(delivered_date__isnull=False) | Q(order__status='delivered'))
        .exclude(status='canceled').exclude(order__status='canceled')
        .exclude(Exists(reviewed))
        .annotate(delivered_on=Coalesce('delivered_date', 'order__date_updated'))
        .select_related('product', 'variant__size', 'variant__color')
        .order_by('-delivered_on')
    )
    seen, out = set(), []
    for line in lines.iterator(chunk_size=200):
        if line.product_id in seen:
            continue
        seen.add(line.product_id)
        out.append(line)
        if len(out) >= limit:
            break
    return out

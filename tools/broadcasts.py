"""
tools/broadcasts.py
A store messages its followers and/or past buyers: new arrivals, a sale,
opening hours. In-app notification for everyone in the audience, plus an email
for those who haven't turned off "Offers from stores".

Guardrails, so shoppers keep wanting these:
  - a monthly allowance per plan (ToolUsage 'broadcasts'), and one per store per day;
  - the same moderation as chat (chat/moderation.py): no phone numbers, emails,
    outside links or payment outside Negromart;
  - every email carries the one-click unsubscribe.

Delivery is a Celery task working in batches (bulk inserts for notifications,
one SMTP connection per email batch), so a big audience doesn't hold a request.
"""
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import Broadcast

logger = logging.getLogger(__name__)

BATCH = 500
BUYERS_WINDOW_DAYS = 365
MIN_GAP = timedelta(hours=24)
MAX_PRODUCTS = 4


class BroadcastError(ValueError):
    """Message is safe to show to the seller."""


def audience_ids(vendor, audience):
    """User ids in the audience (active accounts only), excluding the store's own team."""
    from order.models import OrderProduct
    from vendor.models import VendorMember

    ids = set()
    if audience in (Broadcast.FOLLOWERS, Broadcast.EVERYONE):
        ids |= set(vendor.followers.filter(is_active=True).values_list('pk', flat=True))
    if audience in (Broadcast.BUYERS, Broadcast.EVERYONE):
        since = timezone.now() - timedelta(days=BUYERS_WINDOW_DAYS)
        ids |= set(OrderProduct.objects.filter(product__vendor=vendor, order__is_ordered=True,
                                               date_created__gte=since, order__user__is_active=True)
                   .exclude(status='canceled').values_list('order__user_id', flat=True).distinct())
    team = set(VendorMember.objects.filter(vendor=vendor).values_list('user_id', flat=True))
    return sorted(ids - team - {None})


def sizes(vendor):
    return {a: len(audience_ids(vendor, a)) for a, _ in Broadcast.AUDIENCE_CHOICES}


def next_allowed(vendor):
    last = Broadcast.objects.filter(vendor=vendor).exclude(status=Broadcast.FAILED).order_by('-created_at').first()
    return last.created_at + MIN_GAP if last and last.created_at + MIN_GAP > timezone.now() else None


def create(vendor, user, audience, subject, message, product_ids):
    """Check and queue a broadcast. Raises BroadcastError."""
    from chat import moderation
    from payments.entitlements import plan_for_vendor
    from product.models import Product
    from tools import registry
    from tools.models import ToolUsage

    if audience not in dict(Broadcast.AUDIENCE_CHOICES):
        raise BroadcastError('Choose who gets it.')
    subject = ' '.join(str(subject or '').split())[:80]
    message = str(message or '').strip()[:1000]
    if len(subject) < 4:
        raise BroadcastError('Write a subject (at least 4 characters).')
    if len(message) < 10:
        raise BroadcastError('Write a message (at least 10 characters).')
    found = moderation.check(f'{subject}\n{message}')
    if found:
        raise BroadcastError(moderation.explain(found))
    ids = [int(i) for i in (product_ids or []) if str(i).isdigit()][:MAX_PRODUCTS]
    if ids and Product.objects.filter(vendor=vendor, pk__in=ids, status='published').count() != len(set(ids)):
        raise BroadcastError('Feature only your own live products.')
    wait = next_allowed(vendor)
    if wait:
        raise BroadcastError(f'One message a day: you can send the next one after {timezone.localtime(wait):%d %b, %H:%M}.')
    if not audience_ids(vendor, audience):
        raise BroadcastError('Nobody in this audience yet.')
    limit = registry.allowance(plan_for_vendor(vendor), 'broadcasts')
    if not ToolUsage.take(vendor, 'broadcasts', limit):
        raise BroadcastError(f'You have sent all {limit} messages for this month. They renew on the 1st.')
    broadcast = Broadcast.objects.create(vendor=vendor, created_by=user, audience=audience, subject=subject,
                                         message=message, product_ids=ids)
    from tools.tasks import send_broadcast
    transaction.on_commit(lambda: send_broadcast.delay(broadcast.pk))
    return broadcast


def _products(broadcast):
    from engagement.emails import absolute_media, site_url
    from product.models import Product
    out = []
    for p in Product.objects.filter(pk__in=broadcast.product_ids, status='published'):
        try:
            image = p.image.url if p.image else ''
        except ValueError:
            image = ''
        out.append({'title': p.title, 'price': p.price, 'image_url': absolute_media(image),
                    'url': f'{site_url()}/{p.sku}/{p.slug}?utm_source=store_broadcast&utm_medium=email'})
    return out


def deliver(broadcast_id):
    """Send one queued broadcast (Celery). Safe to run twice: only a queued broadcast is sent."""
    from engagement.emails import base_context, build_email, opted_out_user_ids, send_batch, site_url
    from engagement.models import EmailOptOut
    from notification.models import Notification
    from notification.utils import VERB_META

    claimed = Broadcast.objects.filter(pk=broadcast_id, status=Broadcast.QUEUED).update(status=Broadcast.SENDING)
    if not claimed:
        return 0
    b = Broadcast.objects.select_related('vendor').get(pk=broadcast_id)
    store = b.vendor
    store_path = f'/seller/{store.slug}'
    products = _products(b)
    meta = VERB_META.get('customer_store_news', {})
    ids = audience_ids(store, b.audience)
    User = get_user_model()
    total = emailed = 0
    try:
        for start in range(0, len(ids), BATCH):
            chunk = ids[start:start + BATCH]
            users = list(User.objects.filter(pk__in=chunk, is_active=True).only('pk', 'email', 'first_name'))
            Notification.objects.bulk_create([Notification(recipient=u, verb='customer_store_news', data={
                **meta, 'title': f'{store.name}: {b.subject}', 'message': b.message[:200], 'url': store_path,
                'store': store.name}) for u in users], batch_size=BATCH)
            blocked = opted_out_user_ids([u.pk for u in users], EmailOptOut.STORE_OFFERS)
            messages = []
            for u in users:
                if u.email and u.pk not in blocked:
                    ctx = {**base_context(u, EmailOptOut.STORE_OFFERS), 'store_name': store.name,
                           'store_url': f'{site_url()}{store_path}', 'headline': b.subject,
                           'intro': f'news from {store.name}.', 'body': b.message, 'products': products,
                           'button_text': 'Visit the store'}
                    messages.append(build_email(u, f'{store.name}: {b.subject}', 'store_offer', ctx))
            emailed += send_batch(messages)
            total += len(users)
    except Exception:
        logger.exception('Broadcast %s failed after %s recipients', b.pk, total)
        Broadcast.objects.filter(pk=b.pk).update(status=Broadcast.FAILED, recipients=total, emailed=emailed)
        if not total:      # nobody got it: the store keeps its monthly allowance
            from tools.models import ToolUsage
            ToolUsage.give_back(store, 'broadcasts')
        raise
    Broadcast.objects.filter(pk=b.pk).update(status=Broadcast.SENT, recipients=total, emailed=emailed,
                                             sent_at=timezone.now())
    return total

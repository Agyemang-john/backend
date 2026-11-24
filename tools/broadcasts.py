"""
tools/broadcasts.py
A store messages its followers and/or past buyers: new arrivals, a sale,
opening hours. In-app notification for everyone in the audience, an email for
those who haven't turned off "Offers from stores", and optionally an SMS.

Guardrails, so shoppers keep wanting these:
  - a monthly allowance per plan (ToolUsage 'broadcasts'), and one per store per day;
  - the same moderation as chat (chat/moderation.py): no phone numbers, emails,
    outside links or payment outside Negromart;
  - every email carries the one-click unsubscribe; every SMS a "Stop:" link to
    the shopper's alert settings. Both follow the same "Offers from stores" switch.
  - the store's own team never receives its messages (use "Send me a preview").

SMS (Arkesel, the platform's sender ID) goes only to verified Ghanaian numbers,
one text per shopper, from a separate monthly SMS allowance
(SubscriptionPlan.broadcast_sms_monthly_limit). The allowance is reserved when
the message is queued and whatever wasn't sent is given back.

Delivery is a Celery task working in batches (bulk inserts for notifications,
one SMTP connection per email batch, SMS_BATCH numbers per SMS request). A
beat job (requeue_stuck) picks up messages left queued, e.g. while the worker
was down, and closes ones that stopped half-way.
"""
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import Broadcast

logger = logging.getLogger(__name__)

BATCH = 500
SMS_BATCH = 100
SMS_MAX_CHARS = 306          # two SMS parts
BUYERS_WINDOW_DAYS = 365
MIN_GAP = timedelta(hours=24)
MAX_PRODUCTS = 4
PREVIEWS_PER_DAY = 10
SIZES_CACHE = 60
STUCK_QUEUED = timedelta(minutes=3)
STUCK_SENDING = timedelta(hours=1)


class BroadcastError(ValueError):
    """Message is safe to show to the seller."""


# ── Audience ─────────────────────────────────────────────────────────────────

def _groups(vendor):
    """(followers, buyers) as sets of user ids, active accounts only, without the store's own team."""
    from order.models import OrderProduct
    from vendor.models import VendorMember

    team = set(VendorMember.objects.filter(vendor=vendor).values_list('user_id', flat=True)) | {vendor.user_id}
    followers = set(vendor.followers.filter(is_active=True).values_list('pk', flat=True))
    since = timezone.now() - timedelta(days=BUYERS_WINDOW_DAYS)
    buyers = set(OrderProduct.objects.filter(product__vendor=vendor, order__is_ordered=True, date_created__gte=since,
                                             order__user__is_active=True)
                 .exclude(status='canceled').values_list('order__user_id', flat=True).distinct())
    return followers - team - {None}, buyers - team - {None}


def _pick(groups, audience):
    followers, buyers = groups
    return {Broadcast.FOLLOWERS: followers, Broadcast.BUYERS: buyers}.get(audience, followers | buyers)


def audience_ids(vendor, audience):
    return sorted(_pick(_groups(vendor), audience))


def ghana_number(raw):
    """'+233 24 412 3456' / '0244123456' → '233244123456'; anything else → None."""
    digits = re.sub(r'\D', '', str(raw or ''))
    if digits.startswith('233') and len(digits) == 12:
        return digits
    if digits.startswith('0') and len(digits) == 10:
        return '233' + digits[1:]
    return None


def sms_numbers(user_ids):
    """{user id: number} for shoppers SMS can reach: verified Ghana number, store offers not turned off."""
    from engagement.emails import opted_out_user_ids
    from engagement.models import EmailOptOut

    User = get_user_model()
    out = {}
    user_ids = list(user_ids)
    for start in range(0, len(user_ids), 2000):
        chunk = user_ids[start:start + 2000]
        blocked = opted_out_user_ids(chunk, EmailOptOut.STORE_OFFERS)
        for pk, phone in (User.objects.filter(pk__in=chunk, phone_verified_at__isnull=False)
                          .values_list('pk', 'phone')):
            number = ghana_number(phone)
            if number and pk not in blocked:
                out[pk] = number
    return out


def sizes_key(vendor_id):
    return f'tools:broadcast_sizes:{vendor_id}'


def forget_sizes(vendor_id):
    """Someone followed/unfollowed: the seller page must not keep showing the old count (it blocks Send at 0)."""
    cache.delete(sizes_key(vendor_id))


def sizes(vendor):
    """People per audience, and how many of them SMS can reach (cached a minute)."""
    key = sizes_key(vendor.pk)
    found = cache.get(key)
    if found is None:
        groups = _groups(vendor)
        reachable = set(sms_numbers(groups[0] | groups[1]))
        found = {a: {'people': len(_pick(groups, a)), 'sms': len(_pick(groups, a) & reachable)}
                 for a, _ in Broadcast.AUDIENCE_CHOICES}
        cache.set(key, found, SIZES_CACHE)
    return found


# ── Writing ──────────────────────────────────────────────────────────────────

def store_url(vendor):
    """The store's short link when it has one (Store link tool), else its page."""
    from engagement.emails import site_url
    from payments.entitlements import plan_for_vendor
    from tools import registry
    link = getattr(vendor, 'store_link', None)        # no link: RelatedObjectDoesNotExist is an AttributeError
    if link and registry.included(plan_for_vendor(vendor), 'store_link'):
        return f"{site_url()}/s/{link.handle}"
    return f"{site_url()}/seller/{vendor.slug}"


def sms_text(vendor, subject, message):
    """One SMS (up to two parts): '<Store>: <subject>. <message…> <link> Stop: <alerts page>'."""
    from engagement.emails import site_url
    link = store_url(vendor).replace('https://', '').replace('http://', '')
    stop = f" Stop: {site_url().replace('https://', '').replace('http://', '')}/dashboard/alerts"
    head = f"{vendor.name[:30]}: {subject}."
    room = SMS_MAX_CHARS - len(head) - len(link) - len(stop) - 2
    body = ' '.join(message.split())
    if len(body) > room:
        body = body[:max(room - 1, 0)].rstrip() + '…'
    return f"{head} {body} {link}{stop}".replace('  ', ' ')


def _clean(vendor, subject, message, product_ids):
    from chat import moderation
    from product.models import Product

    subject = ' '.join(str(subject or '').split())[:80]
    message = str(message or '').strip()[:1000]
    if len(subject) < 4:
        raise BroadcastError('Write a subject (at least 4 characters).')
    if len(message) < 10:
        raise BroadcastError('Write a message (at least 10 characters).')
    found = moderation.check(f'{subject}\n{message}')
    if found:
        raise BroadcastError(moderation.explain(found))
    ids = list(dict.fromkeys(int(i) for i in (product_ids or []) if str(i).isdigit()))[:MAX_PRODUCTS]
    if ids and Product.objects.filter(vendor=vendor, pk__in=ids, status='published').count() != len(ids):
        raise BroadcastError('Feature only your own live products.')
    return subject, message, ids


def next_allowed(vendor):
    last = Broadcast.objects.filter(vendor=vendor).exclude(status=Broadcast.FAILED).order_by('-created_at').first()
    return last.created_at + MIN_GAP if last and last.created_at + MIN_GAP > timezone.now() else None


def sms_allowance(vendor):
    from payments.entitlements import plan_for_vendor
    from tools.models import ToolUsage
    limit = getattr(plan_for_vendor(vendor), 'broadcast_sms_monthly_limit', 0) or 0
    return limit, ToolUsage.used(vendor, 'broadcast_sms') if limit else 0


def create(vendor, user, audience, subject, message, product_ids, with_sms=False):
    """Check and queue a broadcast. Raises BroadcastError."""
    from payments.entitlements import plan_for_vendor
    from tools import registry
    from tools.models import ToolUsage

    if audience not in dict(Broadcast.AUDIENCE_CHOICES):
        raise BroadcastError('Choose who gets it.')
    subject, message, ids = _clean(vendor, subject, message, product_ids)
    wait = next_allowed(vendor)
    if wait:
        raise BroadcastError(f'One message a day: you can send the next one after {timezone.localtime(wait):%d %b, %H:%M}.')
    people = audience_ids(vendor, audience)
    if not people:
        raise BroadcastError('Nobody in this audience yet. Shoppers join it when they follow your store or buy from '
                             'you. (Your own team never gets your messages: use "Send me a preview" to see one.)')
    sms_count = 0
    if with_sms:
        sms_limit, sms_used = sms_allowance(vendor)
        if not sms_limit:
            raise BroadcastError('Your plan does not include SMS. Turn SMS off, or upgrade to send texts too.')
        sms_count = len(sms_numbers(people))
        if not sms_count:
            raise BroadcastError('Nobody in this audience can get an SMS yet (it needs a verified Ghana phone number). '
                                 'Turn SMS off to send it in the app and by email.')
        if not ToolUsage.take_many(vendor, 'broadcast_sms', sms_count, sms_limit):
            raise BroadcastError(f'This needs {sms_count} SMS and you have {max(sms_limit - sms_used, 0)} left this '
                                 f'month. Turn SMS off or choose a smaller audience.')
    limit = registry.allowance(plan_for_vendor(vendor), 'broadcasts')
    if not ToolUsage.take(vendor, 'broadcasts', limit):
        ToolUsage.give_back_many(vendor, 'broadcast_sms', sms_count)
        raise BroadcastError(f'You have sent all {limit} messages for this month. They renew on the 1st.')
    broadcast = Broadcast.objects.create(vendor=vendor, created_by=user, audience=audience, subject=subject,
                                         message=message, product_ids=ids, with_sms=bool(sms_count),
                                         sms_reserved=sms_count)
    forget_sizes(vendor.pk)
    from tools.tasks import send_broadcast
    transaction.on_commit(lambda: send_broadcast.delay(broadcast.pk))
    return broadcast


# ── Sending ──────────────────────────────────────────────────────────────────

def _products(product_ids):
    from engagement.emails import absolute_media, site_url
    from product.models import Product
    out = []
    for p in Product.objects.filter(pk__in=product_ids, status='published'):
        try:
            image = p.image.url if p.image else ''
        except ValueError:
            image = ''
        out.append({'title': p.title, 'price': p.price, 'image_url': absolute_media(image),
                    'url': f'{site_url()}/{p.sku}/{p.slug}?utm_source=store_broadcast&utm_medium=email'})
    return out


def _email(user, store, subject, message, products, preview=False):
    from engagement.emails import base_context, build_email
    from engagement.models import EmailOptOut
    ctx = {**base_context(user, EmailOptOut.STORE_OFFERS), 'store_name': store.name, 'store_url': store_url(store),
           'headline': subject, 'intro': f'news from {store.name}.', 'body': message, 'products': products,
           'button_text': 'Visit the store'}
    title = f'{store.name}: {subject}'
    return build_email(user, f'[Preview] {title}' if preview else title, 'store_offer', ctx)


def _notification(user, store, subject, message, preview=False):
    """A preview goes to a seller, whose dashboard bell hides customer_* verbs: send it as an announcement."""
    from notification.models import Notification
    from notification.utils import VERB_META
    verb = 'announcement' if preview else 'customer_store_news'
    title = f'{store.name}: {subject}'
    return Notification(recipient=user, verb=verb, data={
        **VERB_META['customer_store_news'], 'title': f'[Preview] {title}' if preview else title,
        'message': message[:200], 'url': f'/seller/{store.slug}', 'store': store.name})


def _create_and_push(notifications):
    """bulk_create skips post_save, so the bells would stay quiet until a reload: ring them here."""
    from notification.models import Notification
    from notification.utils import push_new_notification
    for n in Notification.objects.bulk_create(notifications, batch_size=BATCH):
        push_new_notification(n)


def _send_sms(numbers, text):
    """Returns how many numbers Arkesel accepted; raises on a failed request."""
    from userauths.tasks import _get_sms_client
    sent = 0
    for start in range(0, len(numbers), SMS_BATCH):
        chunk = numbers[start:start + SMS_BATCH]
        response = _get_sms_client().send_sms(sender=settings.ARKESEL_SENDER, message=text, recipients=chunk)
        if response.get('status') != 'success':
            raise RuntimeError(f"SMS provider: {response.get('message') or response}"[:180])
        sent += len(chunk)
    return sent


def deliver(broadcast_id):
    """Send one queued broadcast (Celery). Safe to run twice: only a queued broadcast is sent."""
    from engagement.emails import opted_out_user_ids, send_batch
    from engagement.models import EmailOptOut
    from tools.models import ToolUsage

    claimed = Broadcast.objects.filter(pk=broadcast_id, status=Broadcast.QUEUED).update(status=Broadcast.SENDING)
    if not claimed:
        return 0
    b = Broadcast.objects.select_related('vendor', 'vendor__store_link').get(pk=broadcast_id)
    store = b.vendor
    products = _products(b.product_ids)
    ids = audience_ids(store, b.audience)
    User = get_user_model()
    total = emailed = sms_sent = 0
    error = ''
    try:
        for start in range(0, len(ids), BATCH):
            chunk = ids[start:start + BATCH]
            users = list(User.objects.filter(pk__in=chunk, is_active=True).only('pk', 'email', 'first_name'))
            _create_and_push([_notification(u, store, b.subject, b.message) for u in users])
            blocked = opted_out_user_ids([u.pk for u in users], EmailOptOut.STORE_OFFERS)
            emailed += send_batch([_email(u, store, b.subject, b.message, products)
                                   for u in users if u.email and u.pk not in blocked])
            total += len(users)
    except Exception as exc:
        logger.exception('Broadcast %s failed after %s recipients', b.pk, total)
        Broadcast.objects.filter(pk=b.pk).update(status=Broadcast.FAILED, recipients=total, emailed=emailed,
                                                 error=f'Stopped: {type(exc).__name__}'[:200])
        ToolUsage.give_back_many(store, 'broadcast_sms', b.sms_reserved)
        if not total:      # nobody got it: the store keeps its monthly allowance
            ToolUsage.give_back(store, 'broadcasts')
        raise

    if b.sms_reserved:
        numbers = list(sms_numbers(ids).values())[:b.sms_reserved]     # never more than was reserved
        try:
            sms_sent = _send_sms(numbers, sms_text(store, b.subject, b.message))
        except Exception as exc:   # app and email went out; report the SMS problem, keep the rest
            logger.exception('Broadcast %s SMS failed', b.pk)
            error = f'SMS not sent: {exc}'[:200]
        ToolUsage.give_back_many(store, 'broadcast_sms', b.sms_reserved - sms_sent)

    Broadcast.objects.filter(pk=b.pk).update(status=Broadcast.SENT, recipients=total, emailed=emailed,
                                             sms_sent=sms_sent, error=error, sent_at=timezone.now())
    return total


def preview(vendor, user, subject, message, product_ids):
    """Send the message to the seller only (app + email): no allowance used, PREVIEWS_PER_DAY a day."""
    subject, message, ids = _clean(vendor, subject, message, product_ids)
    key = f'tools:broadcast_previews:{vendor.pk}:{timezone.localdate()}'
    cache.add(key, 0, 86400)
    if cache.incr(key) > PREVIEWS_PER_DAY:
        raise BroadcastError(f'Up to {PREVIEWS_PER_DAY} previews a day. Try again tomorrow.')
    from engagement.emails import send_batch
    _create_and_push([_notification(user, vendor, subject, message, preview=True)])
    emailed = send_batch([_email(user, vendor, subject, message, _products(ids), preview=True)]) if user.email else 0
    return {'emailed': bool(emailed), 'email': user.email, 'sms_text': sms_text(vendor, subject, message)}


def requeue_stuck(now=None):
    """Beat job: send messages left queued (worker was down), close ones that stopped half-way."""
    from tools.models import ToolUsage
    from tools.tasks import send_broadcast

    now = now or timezone.now()
    queued = list(Broadcast.objects.filter(status=Broadcast.QUEUED, created_at__lt=now - STUCK_QUEUED)
                  .values_list('pk', flat=True)[:200])
    for pk in queued:
        send_broadcast.delay(pk)
    stopped = 0
    for b in Broadcast.objects.filter(status=Broadcast.SENDING, created_at__lt=now - STUCK_SENDING).select_related('vendor'):
        if Broadcast.objects.filter(pk=b.pk, status=Broadcast.SENDING).update(
                status=Broadcast.FAILED, error='Stopped before it finished. Please send it again.'):
            stopped += 1
            ToolUsage.give_back_many(b.vendor, 'broadcast_sms', b.sms_reserved)
            if not b.recipients:
                ToolUsage.give_back(b.vendor, 'broadcasts')
    return {'requeued': len(queued), 'stopped': stopped}

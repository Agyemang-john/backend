"""Choosing a station, parcel lifecycle and station hand-over."""
import math
import secrets
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import PickupParcel, PickupPoint, PickupPointStaff

MAX_CODE_ATTEMPTS = 5


class PickupError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def active_points():
    points = cache.get('pickup:points')
    if points is None:
        points = list(PickupPoint.objects.filter(is_active=True).order_by('town', 'name'))
        cache.set('pickup:points', points, 600)
    return points


def nearby(lat=None, lon=None, q='', limit=30):
    q = (q or '').strip().lower()
    rows = []
    for p in active_points():
        if q and q not in f"{p.name} {p.town} {p.region} {p.address}".lower():
            continue
        dist = _km(lat, lon, p.latitude, p.longitude) if lat is not None and lon is not None else None
        rows.append((dist, p))
    rows.sort(key=lambda r: (r[0] is None, r[0] or 0, r[1].town, r[1].name))
    return rows[:limit]


@transaction.atomic
def select_point(user, point):
    """Make this station the shopper's delivery address. Returns the Address."""
    from address.models import Address
    from userauths.models import Profile

    if not point.is_active:
        raise PickupError("This pickup point isn't taking parcels right now.")
    current = Address.objects.filter(user=user, status=True).first()
    contact = current or Address.objects.filter(user=user).order_by('-modified_at').first()
    full_name = (getattr(contact, 'full_name', '') or f"{user.first_name} {user.last_name}").strip()
    mobile = getattr(contact, 'mobile', '') or getattr(user, 'phone', '') or ''
    if not mobile:
        raise PickupError("Add a phone number first, so the station can reach you.")
    addr = Address.objects.filter(user=user, pickup_point=point).first() or Address(user=user, pickup_point=point)
    addr.full_name = full_name[:50]
    addr.mobile = mobile
    addr.email = getattr(contact, 'email', '') or user.email
    addr.country = point.country
    addr.region = point.region[:30]
    addr.town = point.town[:30]
    addr.address = f"Pickup: {point.name}, {point.address}"[:300]
    addr.latitude, addr.longitude = point.latitude, point.longitude
    Address.objects.filter(user=user, status=True).exclude(pk=addr.pk).update(status=False)
    addr.status = True
    addr.save()
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.address, profile.country, profile.mobile = addr.address, addr.country, addr.mobile
    profile.latitude, profile.longitude = addr.latitude, addr.longitude
    profile.save()
    return addr


def _new_code(point):
    for _ in range(20):
        code = f"{secrets.randbelow(10 ** 6):06d}"
        if not PickupParcel.objects.filter(point=point, code=code,
                                           status__in=(PickupParcel.AWAITING, PickupParcel.READY)).exists():
            return code
    raise PickupError("Could not create a collection code.")


def create_parcel(order):
    point = getattr(order.address, 'pickup_point', None) if order.address_id else None
    if point is None:
        return None
    parcel, _ = PickupParcel.objects.get_or_create(order=order, defaults={'point': point, 'code': _new_code(point)})
    return parcel


def mark_ready(parcel):
    if parcel.status != PickupParcel.AWAITING:
        return parcel
    now = timezone.now()
    parcel.status, parcel.ready_at = PickupParcel.READY, now
    parcel.expires_at = now + timedelta(days=parcel.point.hold_days)
    parcel.save(update_fields=['status', 'ready_at', 'expires_at'])
    _notify_ready(parcel)
    return parcel


def collect(staff_user, code, order_number=''):
    """Station hands a parcel over after checking the shopper's code."""
    points = list(PickupPointStaff.objects.filter(user=staff_user).values_list('point_id', flat=True))
    if not points:
        raise PickupError("You're not set up for a pickup station.", 403)
    code = (code or '').strip()
    with transaction.atomic():
        parcel = (PickupParcel.objects.select_for_update().select_related('order', 'point')
                  .filter(point_id__in=points, code=code, status=PickupParcel.READY).first())
        if parcel is None:
            raise PickupError("No parcel is waiting with that code.", 404)
        if order_number and parcel.order.order_number != order_number.strip():
            parcel.failed_attempts += 1
            parcel.save(update_fields=['failed_attempts'])
            raise PickupError("That code belongs to a different order.", 409)
        parcel.status, parcel.collected_at, parcel.collected_by_staff = PickupParcel.COLLECTED, timezone.now(), staff_user
        parcel.save(update_fields=['status', 'collected_at', 'collected_by_staff'])
    _notify(parcel, f"You collected order {parcel.order.order_number} from {parcel.point.name}. Enjoy!")
    return parcel


def expire_uncollected(now=None):
    now = now or timezone.now()
    n = 0
    for parcel in PickupParcel.objects.filter(status=PickupParcel.READY, expires_at__lt=now).select_related('order', 'point'):
        parcel.status = PickupParcel.EXPIRED
        parcel.save(update_fields=['status'])
        _notify(parcel, f"Order {parcel.order.order_number} wasn't collected from {parcel.point.name} in time and will be "
                        "returned. Contact support if you still want it.")
        n += 1
    # A day before expiry: remind.
    soon = PickupParcel.objects.filter(status=PickupParcel.READY, expires_at__gte=now,
                                       expires_at__lt=now + timedelta(days=1)).select_related('order', 'point')
    for parcel in soon:
        if cache.add(f"pickup:remind:{parcel.pk}", 1, 2 * 86400):
            _notify(parcel, f"Last day to collect order {parcel.order.order_number} from {parcel.point.name}. "
                            f"Your code is {parcel.code}.")
    return n


def _notify(parcel, message):
    from notification.utils import send_notification
    if parcel.order.user_id:
        send_notification(parcel.order.user, 'customer_tracking_update', target=parcel.order,
                          data={'message': message, 'url': f"/dashboard/order-history/{parcel.order_id}"})


def _notify_ready(parcel):
    p = parcel.point
    _notify(parcel, f"Your order {parcel.order.order_number} is ready at {p.name}, {p.address}. "
                    f"Show code {parcel.code} to collect it by {parcel.expires_at:%d %b}.")
    user = parcel.order.user
    phone = getattr(parcel.order.address, 'mobile', '') if parcel.order.address_id else ''
    if user and phone:
        try:
            from django.conf import settings

            from userauths.arkesel_client import ArkeselSMS
            ArkeselSMS().send_sms(sender=settings.ARKESEL_SENDER, recipients=[phone],
                                  message=f"Negromart: order {parcel.order.order_number} is ready at {p.name}. "
                                          f"Code {parcel.code}. Collect by {parcel.expires_at:%d %b}.")
        except Exception:  # noqa: BLE001 - SMS is best effort
            import logging
            logging.getLogger(__name__).exception("pickup ready SMS failed for parcel %s", parcel.pk)

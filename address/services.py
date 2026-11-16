"""
address/services.py

Every change to a shopper's address book goes through here, so the rules
hold whichever screen (header, checkout, dashboard, pickup) made it:

- Orders keep the address they shipped to. An address with orders is never
  edited in place: the edit archives it and saves a new row (`replaced_by`).
  Deleting it archives it too. Invoices, couriers, sellers and disputes keep
  reading `order.address` and always see what the order went to.
- One default (`status`), and a book that has addresses always has one: the
  first address becomes the default, and deleting the default promotes the
  most recently used one. The default is mirrored onto the Profile.
- Saving the same place twice updates the first copy instead of duplicating.
- Coordinates: the device / pin / search result when the shopper gave one,
  else we look the typed address up (address/geo.py).
"""

import logging
import re

from django.db import transaction
from django.utils import timezone

from . import geo
from .models import Address

logger = logging.getLogger(__name__)

MAX_ADDRESSES = 20

# Fields a shopper edits. Changing any of the location ones moves the pin.
EDITABLE = (
    'label', 'full_name', 'mobile', 'email', 'country', 'country_code', 'region', 'town', 'address',
    'address_line2', 'landmark', 'gps_address', 'delivery_instructions', 'latitude', 'longitude',
    'location_source',
)
LOCATION_FIELDS = ('country_code', 'region', 'town', 'address', 'address_line2')


class AddressLimitReached(Exception):
    pass


def book(user):
    return Address.objects.active().filter(user=user)


def has_orders(address):
    from order.models import Order
    return Order.objects.filter(address=address).exists()


def _same(a, b):
    # None and '' are the same blank; numbers compare as numbers.
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-7 or a is b is None
    return (a or '') == (b or '')


def _norm(value):
    return re.sub(r'[\W_]+', ' ', (value or '').lower()).strip()


def _fingerprint(values):
    return tuple(_norm(values.get(f)) for f in ('address', 'address_line2', 'town', 'country_code'))


def find_duplicate(user, data, exclude_pk=None):
    target = _fingerprint(data)
    if not target[0]:
        return None
    for a in book(user).filter(pickup_point__isnull=True).exclude(pk=exclude_pk):
        if _fingerprint({f: getattr(a, f) for f in ('address', 'address_line2', 'town', 'country_code')}) == target:
            return a
    return None


def _locate(address, data, moved):
    """Fill in coordinates. `data` holds what the shopper sent, `moved` whether the place changed."""
    sent_point = data.get('latitude') is not None and data.get('longitude') is not None
    if sent_point:
        address.location_source = data.get('location_source') or Address.SOURCE_PIN
        return
    if not moved and address.location_source:
        return
    queries = [
        ', '.join(p for p in (address.address, address.town, address.region, address.country) if p),
        ', '.join(p for p in (address.town, address.region, address.country) if p),
    ]
    for q in queries:
        found = geo.geocode(q, country_code=address.country_code or None) if q else None
        if found:
            address.latitude, address.longitude = found[0], found[1]
            address.location_source = Address.SOURCE_GEOCODED
            return
    # Nothing matched: keep whatever stand-in we have and say so, so the UI
    # can ask for a pin and nobody mistakes it for the real spot.
    address.location_source = Address.SOURCE_APPROX
    if address.latitude is None or address.longitude is None:
        field = Address._meta
        address.latitude = field.get_field('latitude').default
        address.longitude = field.get_field('longitude').default


def _sync_profile(user, address):
    from userauths.models import Profile
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.address = address.address
    profile.country = address.country
    profile.mobile = address.mobile
    profile.latitude = address.latitude
    profile.longitude = address.longitude
    profile.save()


@transaction.atomic
def set_default(user, address):
    Address.objects.filter(user=user, status=True).exclude(pk=address.pk).update(status=False)
    if not address.status:
        address.status = True
        address.save(update_fields=['status', 'modified_at'])
    _sync_profile(user, address)
    return address


@transaction.atomic
def create(user, data):
    """Returns (address, created). A repeat of a saved place updates that one."""
    data = {k: v for k, v in data.items() if k in EDITABLE or k == 'status'}
    duplicate = find_duplicate(user, data)
    if duplicate is not None:
        return update(user, duplicate, data), False

    if book(user).count() >= MAX_ADDRESSES:
        raise AddressLimitReached()

    make_default = bool(data.pop('status', False)) or not book(user).filter(status=True).exists()
    address = Address(user=user, **{k: v for k, v in data.items() if k in EDITABLE})
    _locate(address, data, moved=True)
    address.save()
    if make_default:
        set_default(user, address)
    return address, True


@transaction.atomic
def update(user, address, data):
    """Apply an edit. Returns the address now in the book (a new row if the old one was used)."""
    make_default = bool(data.get('status'))
    changes = {k: v for k, v in data.items() if k in EDITABLE and not _same(getattr(address, k), v)}
    if not changes:
        if make_default:
            set_default(user, address)
        return address

    moved = any(f in changes for f in LOCATION_FIELDS)
    if not moved and 'latitude' not in changes and 'longitude' not in changes:
        # Contact/label edits keep the existing pin.
        data = {k: v for k, v in data.items() if k not in ('latitude', 'longitude', 'location_source')}
        changes = {k: v for k, v in changes.items() if k not in ('latitude', 'longitude', 'location_source')}

    target = address
    if has_orders(address):
        was_default = address.status
        target = Address(user=user, pickup_point=address.pickup_point, last_used_at=address.last_used_at,
                         **{f: getattr(address, f) for f in EDITABLE})
        # Archive first: only one row may be the default.
        Address.objects.filter(pk=address.pk).update(status=False, archived_at=timezone.now())
        make_default = make_default or was_default

    for k, v in changes.items():
        setattr(target, k, v)
    _locate(target, data, moved=moved)
    target.save()

    if target.pk != address.pk:
        Address.objects.filter(pk=address.pk).update(replaced_by=target)
    if make_default or target.status:
        set_default(user, target)
    return target


@transaction.atomic
def remove(user, address):
    """Delete (or archive, if orders used it). Returns the default afterwards, or None."""
    was_default = address.status
    if has_orders(address):
        Address.objects.filter(pk=address.pk).update(status=False, archived_at=timezone.now())
    else:
        address.delete()

    current = book(user).filter(status=True).first()
    if current is None and was_default:
        current = book(user).order_by('-last_used_at', '-date_added').first()
        if current is not None:
            set_default(user, current)
    return current


def mark_used(address_id):
    Address.objects.filter(pk=address_id).update(last_used_at=timezone.now())

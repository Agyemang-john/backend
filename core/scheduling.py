"""
core/scheduling.py
Which hero slides and side banners the homepage shows right now.

Each slide/banner is either (core.models.Scheduled):
  - seasonal: it has dates, an occasion or a holiday, and shows only while
    that is running (occasions follow their own dates and countries; a
    holiday matches whichever year's occasion the calendar opened for it), or
  - everyday: no schedule at all.

While any seasonal item is live, only those show, plus everyday items marked
"Keep during seasons". Otherwise the everyday ones show. The storefront adds
a built-in slide if this still comes back empty (everything switched off).

Rows are cached once for everyone; picking happens per request because it
depends on the time and the shopper's country.
"""
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

LIVE_SEASONS_TTL = 300   # occasion dates are days long; five minutes late is fine

def schedule_of(obj):
    """The fields picking needs, kept next to the serialized row in the cache."""
    return {
        'starts_at': obj.starts_at.isoformat() if obj.starts_at else None,
        'ends_at': obj.ends_at.isoformat() if obj.ends_at else None,
        'occasion_id': obj.occasion_id,
        'holiday_id': obj.holiday_id,
        'keep_in_season': obj.keep_in_season,
        'order': obj.order,
    }


def live_seasons(country, today=None):
    """
    {'occasions': {id: season}, 'holidays': {event_id: season}} for occasions
    live today in `country`, where season = {title, accent_color, icon}.
    Same rule as the occasions API (product/views.OccasionListAPIView).
    """
    from product.models import Occasion
    today = today or timezone.localdate()
    key = f'live_seasons:{country}:{today.isoformat()}'
    cached = cache.get(key)
    if cached is not None:
        return cached
    rows = (Occasion.objects.filter(is_active=True)
            .filter(Q(start_date__isnull=True) | Q(start_date__lte=today))
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .filter(Q(countries__isnull=True) | Q(countries__code__iexact=country))
            .distinct().order_by('position')
            .values('id', 'event_id', 'title', 'accent_color', 'icon'))
    seasons = {'occasions': {}, 'holidays': {}}
    for row in rows:
        season = {'title': row['title'], 'accent_color': row['accent_color'], 'icon': row['icon']}
        seasons['occasions'][row['id']] = season
        if row['event_id']:
            seasons['holidays'].setdefault(row['event_id'], season)
    cache.set(key, seasons, LIVE_SEASONS_TTL)
    return seasons


def _parse(value):
    from django.utils.dateparse import parse_datetime
    return parse_datetime(value) if value else None


def pick(entries, seasons, now=None):
    """
    entries: [(data, schedule)] in admin order. Returns the data dicts to show,
    each with `season` ({title, accent_color, icon} or None).
    """
    now = now or timezone.now()
    # Cached keys may come back as strings (JSON-serialising caches).
    occasions = {int(k): v for k, v in seasons['occasions'].items()}
    holidays = {int(k): v for k, v in seasons['holidays'].items()}
    seasonal, everyday = [], []
    for data, sched in entries:
        starts, ends = _parse(sched['starts_at']), _parse(sched['ends_at'])
        if starts and starts > now:
            continue
        if ends and ends <= now:
            continue
        season = None
        if sched['occasion_id'] or sched['holiday_id']:
            season = occasions.get(sched['occasion_id']) or holidays.get(sched['holiday_id'])
            if season is None:
                continue          # its occasion/holiday isn't running (here)
        elif not (starts or ends):
            everyday.append({**data, 'season': None, '_keep': sched['keep_in_season']})
            continue
        seasonal.append({**data, 'season': season})

    if seasonal:
        shown = seasonal + [e for e in everyday if e['_keep']]
    else:
        shown = everyday
    for item in shown:
        item.pop('_keep', None)
    return shown

"""
address/geo.py

Address search ("type-ahead"), place lookup and reverse geocoding, done on
the server so every shopper gets the same cached, rate-limited service and
no browser talks to a map provider directly.

Providers (settings.ADDRESS_GEO_PROVIDER, or picked automatically):
- 'google' : Places API (New) autocomplete + details, Geocoding API reverse.
             Used when GOOGLE_MAPS_API_KEY is set. Best coverage in Ghana.
- 'photon' : Komoot Photon (OpenStreetMap). Free, built for type-ahead, the
             default. Point PHOTON_URL at a self-hosted instance at scale.

Every place comes back in one shape (`Place` dict):
    ref, title, subtitle, line1, town, region, country, country_code,
    postal_code, latitude, longitude, precision ('exact' | 'street' | 'area')

Search results carry a `ref`; `resolve(ref)` returns the full place. Photon
gives the full place up front, so its refs point at a cached copy; Google's
are place ids looked up on demand (that's what Google bills per session).
"""

import hashlib
import json
import logging

import requests
from django.conf import settings
from django.core.cache import cache

from .formats import resolve_country

logger = logging.getLogger(__name__)

TIMEOUT = 4  # seconds; the shopper is waiting on every keystroke
USER_AGENT = 'NegromartAddressService/1.0'

SEARCH_TTL = 60 * 60 * 24 * 7     # same words → same answers for a week
PLACE_TTL = 60 * 60 * 24          # refs live a day
REVERSE_TTL = 60 * 60 * 24 * 30   # ~11 m grid, a month
MISS_TTL = 60 * 10                # remember "nothing found" briefly


class GeoUnavailable(Exception):
    """The provider failed or isn't reachable. The UI falls back to typing."""


class PlaceNotFound(Exception):
    pass


def provider_name():
    name = (getattr(settings, 'ADDRESS_GEO_PROVIDER', '') or '').lower()
    if name in ('google', 'photon'):
        return name
    return 'google' if getattr(settings, 'GOOGLE_MAPS_API_KEY', '') else 'photon'


def _key(*parts):
    raw = json.dumps(parts, sort_keys=True, default=str)
    return 'geo:' + hashlib.sha1(raw.encode()).hexdigest()


def _place(**kw):
    name, code = resolve_country(kw.get('country_code') or kw.get('country'))
    lat, lon = kw.get('latitude'), kw.get('longitude')
    return {
        'ref': kw.get('ref', ''),
        'title': (kw.get('title') or kw.get('line1') or kw.get('town') or '').strip(),
        'subtitle': (kw.get('subtitle') or '').strip(),
        'line1': (kw.get('line1') or '').strip()[:300],
        'town': (kw.get('town') or '').strip()[:80],
        'region': (kw.get('region') or '').strip()[:80],
        'country': name or (kw.get('country') or ''),
        'country_code': code or (kw.get('country_code') or '').upper(),
        'postal_code': (kw.get('postal_code') or '').strip()[:20],
        'latitude': float(lat) if lat is not None else None,
        'longitude': float(lon) if lon is not None else None,
        'precision': kw.get('precision') or 'area',
    }


def _subtitle(*parts, skip=()):
    seen, out = set(skip), []
    for p in parts:
        p = (p or '').strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ', '.join(out)


# ── Photon (OpenStreetMap) ────────────────────────────────────────────────────

def _photon_url(path):
    base = getattr(settings, 'PHOTON_URL', '') or 'https://photon.komoot.io'
    return base.rstrip('/') + path


def _photon_get(path, params):
    try:
        r = requests.get(_photon_url(path), params=params, timeout=TIMEOUT,
                         headers={'User-Agent': USER_AGENT})
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Photon %s failed: %s', path, exc)
        raise GeoUnavailable() from exc


def _photon_place(feature):
    p = feature.get('properties') or {}
    coords = (feature.get('geometry') or {}).get('coordinates') or [None, None]
    street = ' '.join(x for x in (p.get('housenumber'), p.get('street')) if x)
    name = p.get('name') or ''
    if name and street and name.lower() not in street.lower():
        line1 = f'{name}, {street}'
    else:
        line1 = street or name
    town = p.get('city') or p.get('town') or p.get('village') or p.get('locality') or p.get('district') or ''
    region = p.get('state') or p.get('county') or ''
    precision = 'exact' if p.get('housenumber') else ('street' if (street or (name and p.get('osm_key') != 'place')) else 'area')
    title = line1 or town or name
    return _place(
        title=title,
        subtitle=_subtitle(p.get('district') if p.get('district') != town else '', town, region, p.get('country'),
                           skip=(title.lower(),)),
        line1=line1, town=town, region=region,
        country=p.get('country'), country_code=p.get('countrycode'),
        postal_code=p.get('postcode'),
        latitude=coords[1], longitude=coords[0], precision=precision,
    )


def _photon_remember(place):
    ref = 'p:' + hashlib.sha1(json.dumps(place, sort_keys=True).encode()).hexdigest()[:24]
    place['ref'] = ref
    cache.set('geo:ref:' + ref, place, PLACE_TTL)
    return place


def _photon_search(q, country_code, near, lang):
    params = {'q': q, 'limit': 12, 'lang': lang if lang in ('en', 'fr', 'de') else 'en'}
    if near:
        params.update({'lat': near[0], 'lon': near[1]})
    data = _photon_get('/api/', params)
    out, seen = [], set()
    for f in data.get('features') or []:
        place = _photon_place(f)
        if country_code and place['country_code'] and place['country_code'] != country_code:
            continue
        dedupe = (place['title'].lower(), place['subtitle'].lower())
        if dedupe in seen or not place['title']:
            continue
        seen.add(dedupe)
        out.append(_photon_remember(place))
        if len(out) == 6:
            break
    return out


def _photon_reverse(lat, lon, lang):
    data = _photon_get('/reverse', {'lat': lat, 'lon': lon, 'lang': lang if lang in ('en', 'fr', 'de') else 'en'})
    features = data.get('features') or []
    if not features:
        raise PlaceNotFound()
    place = _photon_place(features[0])
    # The shopper's point, not the nearest building's.
    place.update(latitude=float(lat), longitude=float(lon))
    return place


# ── Google ───────────────────────────────────────────────────────────────────

def _google_key():
    return getattr(settings, 'GOOGLE_MAPS_API_KEY', '')


def _google_components(components, long_key, short_key):
    by_type = {}
    for c in components or []:
        for t in c.get('types') or []:
            by_type.setdefault(t, c)

    def get(*types, short=False):
        for t in types:
            c = by_type.get(t)
            if c:
                return c.get(short_key if short else long_key) or ''
        return ''

    street = ' '.join(x for x in (get('street_number'), get('route')) if x)
    return {
        'street': street,
        'has_number': bool(get('street_number')),
        'premise': get('premise', 'establishment', 'point_of_interest'),
        'town': get('locality', 'postal_town', 'sublocality', 'administrative_area_level_2', 'neighborhood'),
        'region': get('administrative_area_level_1'),
        'country': get('country'),
        'country_code': get('country', short=True),
        'postal_code': get('postal_code'),
        'neighbourhood': get('neighborhood', 'sublocality'),
    }


def _google_search(q, country_code, near, lang, session):
    body = {'input': q, 'languageCode': lang or 'en'}
    if country_code:
        body['includedRegionCodes'] = [country_code.lower()]
    if near:
        body['locationBias'] = {'circle': {'center': {'latitude': near[0], 'longitude': near[1]}, 'radius': 50000.0}}
    if session:
        body['sessionToken'] = session
    try:
        r = requests.post('https://places.googleapis.com/v1/places:autocomplete', json=body, timeout=TIMEOUT,
                          headers={'X-Goog-Api-Key': _google_key()})
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Google autocomplete failed: %s', exc)
        raise GeoUnavailable() from exc
    out = []
    for s in data.get('suggestions') or []:
        pred = s.get('placePrediction')
        if not pred:
            continue
        fmt = pred.get('structuredFormat') or {}
        out.append({
            **_place(country_code=country_code),
            'ref': 'g:' + pred.get('placeId', ''),
            'title': ((fmt.get('mainText') or {}).get('text') or (pred.get('text') or {}).get('text') or ''),
            'subtitle': (fmt.get('secondaryText') or {}).get('text') or '',
        })
    return out[:6]


def _google_resolve(place_id, lang, session):
    params = {'languageCode': lang or 'en'}
    if session:
        params['sessionToken'] = session
    try:
        r = requests.get(f'https://places.googleapis.com/v1/places/{place_id}', params=params, timeout=TIMEOUT,
                         headers={'X-Goog-Api-Key': _google_key(),
                                  'X-Goog-FieldMask': 'addressComponents,location,formattedAddress,displayName,types'})
        if r.status_code == 404:
            raise PlaceNotFound()
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Google place details failed: %s', exc)
        raise GeoUnavailable() from exc
    c = _google_components(data.get('addressComponents'), 'longText', 'shortText')
    name = (data.get('displayName') or {}).get('text') or ''
    line1 = c['street']
    if name and name.lower() not in (line1 or '').lower() and name != c['town']:
        line1 = f'{name}, {line1}' if line1 else name
    loc = data.get('location') or {}
    return _place(
        ref='g:' + place_id, title=name or line1, subtitle=data.get('formattedAddress') or '',
        line1=line1 or c['neighbourhood'], town=c['town'], region=c['region'],
        country=c['country'], country_code=c['country_code'], postal_code=c['postal_code'],
        latitude=loc.get('latitude'), longitude=loc.get('longitude'),
        precision='exact' if c['has_number'] else ('street' if line1 else 'area'),
    )


def _google_reverse(lat, lon, lang):
    try:
        r = requests.get('https://maps.googleapis.com/maps/api/geocode/json', timeout=TIMEOUT,
                         params={'latlng': f'{lat},{lon}', 'key': _google_key(), 'language': lang or 'en'})
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Google reverse geocode failed: %s', exc)
        raise GeoUnavailable() from exc
    results = data.get('results') or []
    if not results:
        raise PlaceNotFound()
    c = _google_components(results[0].get('address_components'), 'long_name', 'short_name')
    line1 = c['street'] or c['premise'] or c['neighbourhood']
    return _place(
        title=line1 or c['town'], subtitle=results[0].get('formatted_address') or '',
        line1=line1, town=c['town'], region=c['region'], country=c['country'],
        country_code=c['country_code'], postal_code=c['postal_code'],
        latitude=float(lat), longitude=float(lon), precision='exact',
    )


# ── Public API ───────────────────────────────────────────────────────────────

def search(q, country_code=None, near=None, lang='en', session=None):
    """Up to 6 suggestions for what the shopper has typed so far."""
    q = ' '.join((q or '').split())[:120]
    if len(q) < 3:
        return []
    country_code = (country_code or '').upper()[:2] or None
    # A ~10 km grid: nearby shoppers share cache entries.
    near = (round(near[0], 1), round(near[1], 1)) if near else None
    provider = provider_name()
    key = _key('search', provider, q.lower(), country_code, near, lang)
    cached = cache.get(key)
    if cached is not None:
        if provider == 'photon':  # keep the refs alive as long as the results
            for p in cached:
                cache.set('geo:ref:' + p['ref'], p, PLACE_TTL)
        return cached
    if provider == 'google':
        results = _google_search(q, country_code, near, lang, session)
    else:
        results = _photon_search(q, country_code, near, lang)
    cache.set(key, results, SEARCH_TTL if results else MISS_TTL)
    return results


def resolve(ref, lang='en', session=None):
    """The full place behind a search result's ref."""
    ref = (ref or '').strip()
    if ref.startswith('p:'):
        place = cache.get('geo:ref:' + ref)
        if not place:
            raise PlaceNotFound()
        return place
    if ref.startswith('g:') and len(ref) > 2:
        key = _key('place', ref, lang)
        place = cache.get(key)
        if place is None:
            place = _google_resolve(ref[2:], lang, session)
            cache.set(key, place, PLACE_TTL)
        return place
    raise PlaceNotFound()


def reverse(lat, lon, lang='en'):
    """The address at a point (device location or a map pin)."""
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise PlaceNotFound()
    provider = provider_name()
    key = _key('reverse', provider, round(lat, 4), round(lon, 4), lang)
    place = cache.get(key)
    if place is None:
        place = _google_reverse(lat, lon, lang) if provider == 'google' else _photon_reverse(lat, lon, lang)
        cache.set(key, place, REVERSE_TTL)
    # Cached per grid square, but the pin is where the shopper put it.
    return {**place, 'latitude': lat, 'longitude': lon}


def geocode(text, country_code=None):
    """Best point for a typed address: (lat, lon, precision) or None. Never raises."""
    try:
        results = search(text, country_code=country_code)
        if not results:
            return None
        first = results[0]
        if first.get('latitude') is None:  # Google suggestions need a lookup
            first = resolve(first['ref'])
        if first.get('latitude') is None:
            return None
        return first['latitude'], first['longitude'], first.get('precision') or 'area'
    except (GeoUnavailable, PlaceNotFound):
        return None
    except Exception:  # pragma: no cover - never break a save over a lookup
        logger.exception('Geocoding %r failed', text)
        return None

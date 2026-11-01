"""
Event collection, rollups, experiment assignment and results.

Hot path (every event) touches Redis only: PFADD visitor into a HyperLogLog and
INCR a counter, per step per day (and per running-experiment variant the
visitor is in). flush() persists the counters into DailyMetric every few
minutes. Without Redis (tests, local dev) the same API works on the Django
cache with exact sets.
"""
import hashlib
import math
import re
from datetime import date

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import DailyMetric, Experiment, ExperimentExposure

FUNNEL = ['page_view', 'product_view', 'add_to_cart', 'checkout_start', 'purchase']
ALLOWED = set(FUNNEL) | {'search', 'signup', 'wishlist_add', 'ad_click', 'share'}
_VISITOR_RE = re.compile(r'^[A-Za-z0-9:_-]{8,64}$')
KEY_TTL = 3 * 86400
REGISTRY = 'an:keys'


def _redis():
    try:
        from django_redis import get_redis_connection
        return get_redis_connection('default')
    except Exception:  # noqa: BLE001
        return None


def clean_visitor(raw):
    return raw if raw and _VISITOR_RE.match(raw) else None


# ── Experiments ──────────────────────────────────────────────────────────────

def running_experiments():
    exps = cache.get('an:experiments')
    if exps is None:
        exps = [(e.key, e.variants, e.pk) for e in Experiment.objects.filter(status=Experiment.RUNNING)]
        cache.set('an:experiments', exps, 60)
    return exps


def assign(key, variants, visitor):
    """Deterministic weighted pick: the same visitor always gets the same variant."""
    total = sum(max(int(v.get('weight', 0)), 0) for v in variants) or 1
    bucket = int(hashlib.sha256(f"{key}:{visitor}".encode()).hexdigest()[:8], 16) % total
    upto = 0
    for v in variants:
        upto += max(int(v.get('weight', 0)), 0)
        if bucket < upto:
            return v['key']
    return variants[0]['key']


def assignments(visitor, user=None, record=True):
    """{experiment_key: variant} for every running experiment; records first exposure."""
    out = {}
    for key, variants, exp_id in running_experiments():
        if not variants:
            continue
        variant = assign(key, variants, visitor)
        out[key] = variant
        if record and cache.add(f"an:exp:{exp_id}:{visitor}", 1, 86400):
            try:
                with transaction.atomic():
                    ExperimentExposure.objects.create(experiment_id=exp_id, visitor=visitor, variant=variant,
                                                      user=user if user and user.is_authenticated else None)
            except IntegrityError:
                pass
    return out


def link_user(visitor, user):
    """A visitor signed in: attach their exposures (made while anonymous) to the account."""
    if user is None or not user.is_authenticated:
        return
    if cache.add(f"an:link:{visitor}:{user.pk}", 1, 86400):
        ExperimentExposure.objects.filter(visitor=visitor, user__isnull=True).update(user=user)
        cache.set(f"an:uv:{user.pk}", visitor, 30 * 86400)


# ── Recording ────────────────────────────────────────────────────────────────

def _keys(day, name, exp_variants):
    base = f"an:{day.isoformat()}:{name}::"
    keys = [base]
    keys += [f"an:{day.isoformat()}:{name}:{k}:{v}" for k, v in exp_variants.items()]
    return keys


def record(name, visitor, exp_variants=None, day=None):
    if name not in ALLOWED or not visitor:
        return
    day = day or timezone.localdate()
    keys = _keys(day, name, exp_variants or {})
    conn = _redis()
    if conn is not None:
        try:
            pipe = conn.pipeline()
            for k in keys:
                pipe.pfadd(f"{k}|v", visitor)
                pipe.incr(f"{k}|n")
                pipe.expire(f"{k}|v", KEY_TTL)
                pipe.expire(f"{k}|n", KEY_TTL)
                pipe.sadd(REGISTRY, k)
            pipe.execute()
            return
        except Exception:  # noqa: BLE001 - analytics never breaks a request
            return
    # Fallback: exact sets in the Django cache.
    reg = cache.get(REGISTRY) or set()
    for k in keys:
        visitors = cache.get(f"{k}|v") or set()
        visitors.add(visitor)
        cache.set(f"{k}|v", visitors, KEY_TTL)
        cache.set(f"{k}|n", (cache.get(f"{k}|n") or 0) + 1, KEY_TTL)
        reg.add(k)
    cache.set(REGISTRY, reg, KEY_TTL)


def visitor_experiments(visitor):
    return {key: assign(key, variants, visitor) for key, variants, _ in running_experiments() if variants}


def flush():
    """Persist every counter into DailyMetric (overwrites with the running totals). Returns rows written."""
    conn = _redis()
    if conn is not None:
        keys = [k.decode() if isinstance(k, bytes) else k for k in conn.smembers(REGISTRY)]
        values = []
        for k in keys:
            values.append((k, conn.pfcount(f"{k}|v"), int(conn.get(f"{k}|n") or 0)))
        # Keys for days that have expired out of Redis drop off the registry.
        stale = [k for k, v, n in values if v == 0 and n == 0]
        if stale:
            conn.srem(REGISTRY, *stale)
    else:
        keys = list(cache.get(REGISTRY) or [])
        values = [(k, len(cache.get(f"{k}|v") or ()), cache.get(f"{k}|n") or 0) for k in keys]
    written = 0
    for k, visitors, events in values:
        if not events:
            continue
        _, day, name, exp, variant = k.split(':', 4)
        DailyMetric.objects.update_or_create(
            date=date.fromisoformat(day), name=name, experiment=exp, variant=variant,
            defaults={'visitors': visitors, 'events': events})
        written += 1
    return written


# ── Results ──────────────────────────────────────────────────────────────────

def _p_value(c1, n1, c2, n2):
    """Two-sided two-proportion z-test."""
    if min(n1, n2) == 0:
        return None
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    z = abs(c1 / n1 - c2 / n2) / se
    return math.erfc(z / math.sqrt(2))


def results(experiment):
    """Per variant: exposed, converted (goal after exposure), rate, lift and p-value vs control."""
    from django.db.models import Count, Q
    rows = {r['variant']: r for r in ExperimentExposure.objects.filter(experiment=experiment)
            .values('variant').annotate(exposed=Count('id'), converted=Count('id', filter=Q(converted_at__isnull=False)))}
    variants = [v['key'] for v in experiment.variants]
    control = rows.get(variants[0]) if variants else None
    out = []
    for key in variants:
        r = rows.get(key, {'exposed': 0, 'converted': 0})
        rate = r['converted'] / r['exposed'] if r['exposed'] else 0
        entry = {'variant': key, 'exposed': r['exposed'], 'converted': r['converted'], 'rate': round(100 * rate, 2)}
        if control is not None and key != variants[0]:
            base = control['converted'] / control['exposed'] if control['exposed'] else 0
            entry['lift'] = round(100 * (rate - base) / base, 1) if base else None
            entry['p_value'] = _p_value(r['converted'], r['exposed'], control['converted'], control['exposed'])
        out.append(entry)
    return out


def mark_converted(user_id, goal='purchase'):
    """The user reached `goal`: every running experiment they were exposed to counts a conversion (once)."""
    now = timezone.now()
    return ExperimentExposure.objects.filter(user_id=user_id, converted_at__isnull=True,
                                             experiment__status=Experiment.RUNNING, experiment__goal=goal,
                                             first_seen__lte=now).update(converted_at=now)

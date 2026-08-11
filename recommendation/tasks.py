"""
recommendation/tasks.py

Celery entry points. All heavy lifting lives in train.py / deals.py — these
handle scheduling, locking and failure semantics only.

Cadence and the reasoning behind it:

  train_recommender   nightly at 02:30 UTC — a full retrain. Taste moves over
                      days, not minutes, and off-peak keeps the CPU spike away
                      from shoppers.
  score_deals         hourly — deals turn over fast, and a deals page showing
                      sold-out stock is worse than no deals page.
  prune_events        weekly — recommendation events are high volume and only
                      the recent window is useful.

A Redis lock guards training: Beat can fire again while a long run is still
going, and two concurrent runs would fight over the same tables.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

TRAIN_LOCK_KEY = 'rec:train:lock'
TRAIN_LOCK_TTL = 3600          # an hour is far beyond a normal run at this scale


@shared_task(bind=True, max_retries=2, default_retry_delay=600, ignore_result=True)
def train_recommender(self, evaluate: bool = True):
    """
    Retrain the whole recommender: embeddings, neighbours, per-shopper rails.

    Skips rather than queues if a run is already in flight — a second run would
    duplicate work and race the first one's writes.
    """
    from django.core.cache import cache

    if not cache.add(TRAIN_LOCK_KEY, 1, TRAIN_LOCK_TTL):
        logger.info("train_recommender: a run is already in progress — skipping")
        return

    try:
        from .train import run_training
        run = run_training(evaluate=evaluate)
        logger.info(
            "train_recommender: run #%s completed in %.1fs (%s)",
            run.pk, run.duration_seconds or 0.0, run.notes,
        )
        # Let serving pick up the new model on the next request.
        cache.delete('rec:model_version')
        return run.pk
    except Exception as exc:
        logger.error("train_recommender failed: %s", exc)
        raise self.retry(exc=exc)
    finally:
        cache.delete(TRAIN_LOCK_KEY)


@shared_task(bind=True, max_retries=3, default_retry_delay=300, ignore_result=True)
def score_deals(self):
    """Snapshot prices and rescore every deal. Cheap enough to run hourly."""
    try:
        from .train import run_deal_scoring
        count = run_deal_scoring()
        logger.info("score_deals: %d eligible deals", count)
        return count
    except Exception as exc:
        logger.error("score_deals failed: %s", exc)
        raise self.retry(exc=exc)


@shared_task(ignore_result=True)
def log_recommendation_events(events: list[dict]):
    """
    Persist a batch of impressions/clicks from the storefront.

    Batched and async because impressions arrive by the hundred per page view;
    inserting them inline would make the rails slower than the products they
    recommend.
    """
    from django.utils import timezone

    from .models import RecommendationEvent

    if not events:
        return 0

    today = timezone.now().date()
    rows = []
    for event in events[:500]:                # bound the damage from a bad client
        try:
            rows.append(RecommendationEvent(
                user_id=event.get('user_id'),
                visitor_key=(event.get('visitor_key') or '')[:100],
                product_id=int(event['product_id']),
                surface=event['surface'][:30],
                event_type=event['event_type'][:15],
                position=event.get('position'),
                reason=(event.get('reason') or '')[:30],
                model_run_id=event.get('model_run_id'),
                date=today,
            ))
        except (KeyError, TypeError, ValueError):
            continue

    if rows:
        RecommendationEvent.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)
    return len(rows)


@shared_task(ignore_result=True)
def prune_recommendation_events(days: int = 90):
    """Drop event rows past the useful window."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import ProductPriceHistory, RecommendationEvent

    cutoff = timezone.now().date() - timedelta(days=days)
    deleted, _ = RecommendationEvent.objects.filter(date__lt=cutoff).delete()

    # Price history is only read over a 30-day window; a year is plenty of margin.
    price_cutoff = timezone.now().date() - timedelta(days=365)
    price_deleted, _ = ProductPriceHistory.objects.filter(date__lt=price_cutoff).delete()

    logger.info(
        "prune_recommendation_events: removed %d events, %d price rows", deleted, price_deleted,
    )
    return deleted


@shared_task(ignore_result=True)
def report_surface_performance(days: int = 7):
    """
    Log click-through and add-to-cart rate per rail.

    Turns "which rail is working" from an opinion into a number. Written to logs
    rather than a dashboard for now — the data is in RecommendationEvent whenever
    a dashboard is wanted.
    """
    from datetime import timedelta

    from django.db.models import Count, Q
    from django.utils import timezone

    from .models import EVENT_ADD_TO_CART, EVENT_CLICK, EVENT_IMPRESSION, RecommendationEvent

    since = timezone.now().date() - timedelta(days=days)
    rows = (
        RecommendationEvent.objects
        .filter(date__gte=since)
        .values('surface')
        .annotate(
            impressions=Count('id', filter=Q(event_type=EVENT_IMPRESSION)),
            clicks=Count('id', filter=Q(event_type=EVENT_CLICK)),
            cart_adds=Count('id', filter=Q(event_type=EVENT_ADD_TO_CART)),
        )
    )

    report = {}
    for row in rows:
        impressions = row['impressions'] or 0
        report[row['surface']] = {
            'impressions': impressions,
            'ctr': round(row['clicks'] / impressions, 4) if impressions else None,
            'cart_rate': round(row['cart_adds'] / impressions, 4) if impressions else None,
        }
        logger.info(
            "surface %-22s impressions=%-7d ctr=%-8s cart_rate=%s",
            row['surface'], impressions,
            report[row['surface']]['ctr'], report[row['surface']]['cart_rate'],
        )
    return report

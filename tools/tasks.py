import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(soft_time_limit=600, time_limit=660)
def run_repricer():
    """Every 15 minutes (ecommerce/settings.py beat schedule)."""
    from tools.pricing import run_repricer as run
    changed = run()
    if changed:
        logger.info('Repricer changed %d prices', changed)
    return changed


@shared_task(soft_time_limit=3000, time_limit=3300)
def send_broadcast(broadcast_id):
    """Deliver one store message to its audience (tools/broadcasts.py)."""
    from tools.broadcasts import deliver
    return deliver(broadcast_id)


@shared_task(soft_time_limit=3000, time_limit=3300)
def run_promotions():
    """Daily: win-back and thank-you coupons, and the next weekly sales (tools/promotions.py)."""
    from tools.promotions import run_all
    return run_all()


@shared_task(soft_time_limit=3000, time_limit=3300)
def refresh_demand_terms():
    """Nightly: condense the last 28 days of searches for sellers (tools/demand.py)."""
    from tools.demand import refresh
    return refresh()


@shared_task(soft_time_limit=1800, time_limit=1900)
def stock_forecast_alerts():
    """Daily: tell stores (on a plan with the stock forecast) what will run out before a restock arrives."""
    from django.db.models import Q

    from payments.models import SubscriptionPlan, VendorSubscription
    from tools.forecast import alert_vendor
    from vendor.models import Vendor

    plans = list(SubscriptionPlan.objects.filter(can_use_stock_forecast=True).values_list('pk', flat=True))
    vendor_ids = (VendorSubscription.objects.filter(plan_id__in=plans, status__in=VendorSubscription.LIVE_STATUSES)
                  .values_list('vendor_id', flat=True).distinct())
    sent = 0
    for vendor in Vendor.objects.filter(Q(pk__in=vendor_ids)).select_related('user').iterator(chunk_size=200):
        try:
            sent += alert_vendor(vendor)
        except Exception:
            logger.exception('Stock forecast alert failed for vendor %s', vendor.pk)
    return sent


@shared_task(soft_time_limit=60, time_limit=90, ignore_result=True)
def deliver_webhook(delivery_id):
    """Send one webhook delivery; schedules its own retry (tools/webhooks.py)."""
    from tools.webhooks import deliver
    delay = deliver(delivery_id)
    if delay is not None:
        deliver_webhook.apply_async((delivery_id,), countdown=delay)


@shared_task(soft_time_limit=600, time_limit=660)
def purge_webhook_deliveries():
    """Daily: drop webhook delivery logs older than two weeks."""
    from tools.webhooks import purge_old
    return purge_old()


@shared_task(soft_time_limit=120, time_limit=180)
def requeue_stuck_broadcasts():
    """Every few minutes: send store messages left queued, close ones that stopped half-way."""
    from tools.broadcasts import requeue_stuck
    return requeue_stuck()

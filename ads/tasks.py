from celery import shared_task


@shared_task(ignore_result=True)
def flush_ad_impressions():
    """Every 5 min: Redis impression buffer → AdDailyStat; refresh which stores can pay for ads."""
    from .services import flush_impressions, refresh_funded_vendors
    n = flush_impressions()
    refresh_funded_vendors()
    return n


@shared_task(ignore_result=True, soft_time_limit=1800)
def nightly_ads():
    """Bill yesterday's spend to seller ledgers, attribute orders to clicks, prune old clicks."""
    from .services import attribute_orders, bill_spend, prune_clicks
    return {'billed': bill_spend(), 'attributed': attribute_orders(), 'pruned': prune_clicks()}

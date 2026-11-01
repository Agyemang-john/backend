from celery import shared_task


@shared_task(ignore_result=True, expires=280)
def flush_metrics():
    """Every 5 min: Redis counters → DailyMetric."""
    from .collector import flush
    return flush()

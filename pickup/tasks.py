from celery import shared_task


@shared_task(ignore_result=True)
def expire_parcels():
    """Daily: remind on the last day, then flag uncollected parcels for return."""
    from .services import expire_uncollected
    return expire_uncollected()

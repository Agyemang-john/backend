from celery import shared_task


@shared_task(ignore_result=True)
def recompute_items(item_ids):
    from .buybox import recompute
    return recompute(item_ids)


@shared_task(ignore_result=True, soft_time_limit=540, expires=540)
def recompute_all_buyboxes():
    """Every 10 min: catches stock/price changes made by F()/bulk updates (no signals)."""
    from .buybox import recompute_all
    return recompute_all()

from celery import shared_task


@shared_task(ignore_result=True, soft_time_limit=840, expires=840)
def sweep_product_alerts():
    """Every 15 min: price-drop and back-in-stock alerts (engagement/alerts.py)."""
    from .alerts import sweep
    return sweep()


@shared_task(ignore_result=True, soft_time_limit=840, expires=840)
def sweep_cart_reminders():
    """Every 15 min: abandoned-cart reminders (engagement/cart_reminders.py)."""
    from .cart_reminders import sweep
    return sweep()

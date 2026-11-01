from celery import shared_task


@shared_task(ignore_result=True, bind=True, max_retries=3, default_retry_delay=60)
def assess_order(self, order_id):
    from order.models import Order

    from .services import assess
    order = Order.objects.select_related('user', 'address').filter(pk=order_id).first()
    if order is None:
        return None
    try:
        return assess(order).score
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc)

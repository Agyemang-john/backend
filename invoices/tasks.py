from celery import shared_task


@shared_task(ignore_result=True, bind=True, max_retries=3, default_retry_delay=300)
def issue_order_invoices(self, order_id):
    from order.models import Order

    from .services import issue_all
    order = Order.objects.filter(pk=order_id, is_ordered=True).select_related('address', 'user').first()
    if order is None:
        return
    try:
        issue_all(order)
    except Exception as exc:  # noqa: BLE001 - downloads issue lazily anyway
        raise self.retry(exc=exc)

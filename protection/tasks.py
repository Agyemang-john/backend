from celery import shared_task


@shared_task(ignore_result=True)
def claim_deadlines():
    """Hourly: escalate claims the seller didn't answer and offers the shopper never decided on."""
    from .services import run_deadlines
    return run_deadlines()

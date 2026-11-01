import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def notify_new_question(question_id):
    """Tell the store's members who manage the catalogue (owner always)."""
    from notification.utils import send_notification
    from vendor.access import Capability, capabilities_for_role
    from vendor.models import VendorMember

    from .models import ProductQuestion

    q = ProductQuestion.objects.select_related('product__vendor').filter(pk=question_id).first()
    if q is None or q.product.vendor is None:
        return
    members = (VendorMember.objects.filter(vendor=q.product.vendor, is_active=True)
               .select_related('user'))
    for m in members:
        if Capability.MANAGE_CATALOG not in capabilities_for_role(m.role):
            continue
        send_notification(
            recipient=m.user, verb='vendor_new_question', target=q,
            data={'message': f"New question on {q.product.title}: “{q.text[:120]}”",
                  'url': f"/questions?question={q.pk}", 'product_id': q.product_id},
        )


@shared_task(ignore_result=True)
def notify_question_answered(answer_id):
    from notification.utils import send_notification

    from .models import ProductAnswer

    a = ProductAnswer.objects.select_related('question__product', 'question__user', 'vendor').filter(pk=answer_id).first()
    if a is None or a.question.user is None or a.question.user_id == a.user_id:
        return
    p = a.question.product
    who = a.vendor.name if a.vendor else 'A shopper who bought it'
    send_notification(
        recipient=a.question.user, verb='customer_question_answered', target=a.question,
        data={'message': f"{who} answered your question about {p.title}.",
              'url': f"/{p.sku}/{p.slug}#questions", 'product_id': p.pk},
    )

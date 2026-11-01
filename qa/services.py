"""Rules for asking, answering, voting and reporting. Views stay thin; tests hit these directly too."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from chat import moderation

from .models import REPORT_HIDE_THRESHOLD, AnswerVote, ProductAnswer, ProductQuestion, QAReport

MAX_QUESTIONS_PER_DAY = 10
MAX_ANSWERS_PER_DAY = 30


class QAError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _clean(text, min_len, max_len, what):
    text = ' '.join((text or '').split())
    if len(text) < min_len:
        raise QAError(f"Write at least {min_len} characters for your {what}.")
    if len(text) > max_len:
        raise QAError(f"Keep your {what} under {max_len} characters.")
    found = moderation.check(text)
    if found:
        raise QAError(_contact_message(found))
    return text


def _contact_message(found):
    parts = [moderation.LABELS[c] for c in found if c in moderation.LABELS]
    listed = ', '.join(parts) or 'contact details'
    return (f"Not posted: it looks like it contains {listed}. Questions and answers are public, "
            "so keep contact details and payment arrangements off them.")


def seller_membership_for(user, product):
    """The user's store membership if they may answer for this product's store, else None."""
    from vendor.access import Capability, capabilities_for_role, get_membership, signed_in_as_seller
    if not signed_in_as_seller(user):
        return None
    membership = get_membership(user)
    if (membership is None or membership.vendor_id != product.vendor_id
            or Capability.MANAGE_CATALOG not in capabilities_for_role(membership.role)):
        return None
    return membership


def ask(user, product, text):
    if product is None or product.status != 'published':
        raise QAError("Questions can't be asked about this product.", 404)
    text = _clean(text, 8, 300, 'question')
    since = timezone.now() - timedelta(days=1)
    if ProductQuestion.objects.filter(user=user, created_at__gte=since).count() >= MAX_QUESTIONS_PER_DAY:
        raise QAError("You've asked a lot of questions today. Try again tomorrow.", 429)
    if ProductQuestion.objects.filter(product=product, user=user, text__iexact=text).exists():
        raise QAError("You've already asked this question.", 409)
    question = ProductQuestion.objects.create(product=product, user=user, text=text)
    transaction.on_commit(lambda: _notify_seller(question.pk))
    return question


def answer(user, question, text):
    from product.review_services import purchased_line

    if question.status != ProductQuestion.PUBLISHED:
        raise QAError("This question is no longer open.", 404)
    product = question.product
    membership = seller_membership_for(user, product)
    verified = False
    if membership is None:
        verified = purchased_line(user, product) is not None
        if not verified:
            raise QAError("Only the seller and shoppers who received this item can answer.", 403)
    text = _clean(text, 2, 1000, 'answer')
    since = timezone.now() - timedelta(days=1)
    if ProductAnswer.objects.filter(user=user, created_at__gte=since).count() >= MAX_ANSWERS_PER_DAY:
        raise QAError("You've answered a lot today. Try again tomorrow.", 429)

    with transaction.atomic():
        ans = ProductAnswer.objects.create(
            question=question, user=user, text=text, is_verified_buyer=verified,
            vendor=membership.vendor if membership else None,
        )
        updates = {'answer_count': F('answer_count') + 1}
        if membership:
            updates['seller_answered'] = True
        ProductQuestion.objects.filter(pk=question.pk).update(**updates)
    transaction.on_commit(lambda: _notify_asker(ans.pk))
    return ans


def toggle_helpful(user, answer_obj):
    """Returns (voted, helpful_count)."""
    if answer_obj.user_id == user.pk:
        raise QAError("You can't vote on your own answer.")
    with transaction.atomic():
        deleted, _ = AnswerVote.objects.filter(answer=answer_obj, user=user).delete()
        if deleted:
            ProductAnswer.objects.filter(pk=answer_obj.pk, helpful_count__gt=0).update(helpful_count=F('helpful_count') - 1)
            voted = False
        else:
            try:
                with transaction.atomic():
                    AnswerVote.objects.create(answer=answer_obj, user=user)
            except IntegrityError:  # double tap raced
                pass
            else:
                ProductAnswer.objects.filter(pk=answer_obj.pk).update(helpful_count=F('helpful_count') + 1)
            voted = True
    answer_obj.refresh_from_db(fields=['helpful_count'])
    return voted, answer_obj.helpful_count


def report(user, reason, question=None, answer_obj=None):
    target = question or answer_obj
    model = ProductQuestion if question else ProductAnswer
    if target.user_id == user.pk:
        raise QAError("You can't report your own post.")
    if reason not in dict(QAReport.REASONS):
        raise QAError("Choose a reason.")
    try:
        with transaction.atomic():
            QAReport.objects.create(user=user, reason=reason, question=question, answer=answer_obj)
    except IntegrityError:
        raise QAError("You've already reported this. Our team will look at it.", 409)
    model.objects.filter(pk=target.pk).update(report_count=F('report_count') + 1)
    model.objects.filter(pk=target.pk, report_count__gte=REPORT_HIDE_THRESHOLD).update(status=model.HIDDEN)


def delete_answer(user, answer_obj):
    if answer_obj.user_id != user.pk:
        raise QAError("Not found.", 404)
    with transaction.atomic():
        question_id, was_published = answer_obj.question_id, answer_obj.status == ProductAnswer.PUBLISHED
        answer_obj.delete()
        if was_published:
            ProductQuestion.objects.filter(pk=question_id, answer_count__gt=0).update(answer_count=F('answer_count') - 1)
        if not ProductAnswer.objects.filter(question_id=question_id, vendor__isnull=False,
                                            status=ProductAnswer.PUBLISHED).exists():
            ProductQuestion.objects.filter(pk=question_id).update(seller_answered=False)


# ── Notifications (after commit; never fail the request) ────────────────────

def _notify_seller(question_id):
    from .tasks import notify_new_question
    notify_new_question.delay(question_id)


def _notify_asker(answer_id):
    from .tasks import notify_question_answered
    notify_question_answered.delay(answer_id)

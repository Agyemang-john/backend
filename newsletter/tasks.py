# apps/newsletters/tasks.py
from celery import shared_task
from django.db.models import Q
from django.utils import timezone
from django.template import Template, Context
from django.core.mail import EmailMultiAlternatives
from django.conf import settings

from .models import Campaign, CampaignRecipient, Subscriber

def _render_templates(tpl, subscriber, campaign):
    ctx = {
        "first_name": subscriber.first_name or "",
        "last_name": subscriber.last_name or "",
        "email": subscriber.email,
        "preheader": campaign.preheader or "",
        "unsubscribe_url": f"{settings.SITE_URL}/newsletter/unsubscribe/{subscriber.unsubscribe_token}/",
        "manage_prefs_url": f"{settings.SITE_URL}/newsletter/preferences/{subscriber.unsubscribe_token}/",
    }
    subject = Template(tpl.subject_template).render(Context(ctx))
    html    = Template(tpl.html_template).render(Context(ctx))
    text    = Template(tpl.text_template or "").render(Context(ctx)) if tpl.text_template else None
    return subject.strip(), html, (text or "")

def _segment_queryset(campaign: Campaign):
    qs = Subscriber.objects.filter(is_active=True, unsubscribed_at__isnull=True)
    inc = campaign.include_tags.values_list("id", flat=True)
    exc = campaign.exclude_tags.values_list("id", flat=True)
    if inc:
        qs = qs.filter(tags__in=inc).distinct()
    if exc:
        qs = qs.exclude(tags__in=exc).distinct()
    return qs

def build_recipient_list(campaign_id: int) -> int:
    """Create CampaignRecipient rows from current segmentation (idempotent)."""
    campaign = Campaign.objects.get(id=campaign_id)
    subs = _segment_queryset(campaign).values_list("id", flat=True)
    created = 0
    for sid in subs:
        obj, was_created = CampaignRecipient.objects.get_or_create(
            campaign=campaign, subscriber_id=sid
        )
        if was_created:
            created += 1
    return created

@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def enqueue_campaign_send(self, campaign_id: int):
    campaign = Campaign.objects.select_related("template").get(id=campaign_id)
    if campaign.status in ["sent","cancelled"]:
        return

    # Ensure recipient snapshot exists
    if campaign.recipients.count() == 0:
        build_recipient_list(campaign_id)

    campaign.status = "sending"
    campaign.started_at = timezone.now()
    campaign.save(update_fields=["status", "started_at"])

    # Send in batches
    batch = campaign.batch_size or 500
    while True:
        batch_qs = campaign.recipients.filter(sent_at__isnull=True)[:batch]
        if not batch_qs.exists():
            break
        ids = list(batch_qs.values_list("id", flat=True))
        send_campaign_batch.delay(campaign.id, ids)

    # If all done, mark finished later by a small follow-up task
    finalize_campaign.delay(campaign.id)

@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_campaign_batch(self, campaign_id: int, recipient_ids: list[int]):
    campaign = Campaign.objects.select_related("template").get(id=campaign_id)
    tpl = campaign.template

    recs = CampaignRecipient.objects.select_related("subscriber").filter(id__in=recipient_ids)
    for rec in recs:
        sub = rec.subscriber
        try:
            subject, html, text = _render_templates(tpl, sub, campaign)
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text or "",
                from_email=campaign.from_email,
                to=[sub.email],
            )
            msg.attach_alternative(html, "text/html")
            msg.send(fail_silently=False)

            rec.sent_at = timezone.now()
            rec.delivered = True  # mark as delivered optimistically; adjust if you add webhook tracking
            rec.save(update_fields=["sent_at", "delivered"])

        except Exception as e:
            # basic error logging & soft bounce counter
            rec.error = str(e)[:500]
            rec.bounced = True
            rec.save(update_fields=["error", "bounced"])
            sub.bounce_count = (sub.bounce_count or 0) + 1
            sub.save(update_fields=["bounce_count"])

@shared_task
def finalize_campaign(campaign_id: int):
    campaign = Campaign.objects.get(id=campaign_id)
    remaining = campaign.recipients.filter(sent_at__isnull=True).count()
    if remaining == 0 and campaign.status != "cancelled":
        campaign.status = "sent"
        campaign.finished_at = timezone.now()
        campaign.save(update_fields=["status","finished_at"])

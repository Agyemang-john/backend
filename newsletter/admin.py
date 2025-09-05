# apps/newsletters/admin.py
from django.contrib import admin, messages
from django.utils import timezone
from .models import Subscriber, Tag, NewsletterTemplate, Campaign, CampaignRecipient
from .tasks import enqueue_campaign_send, build_recipient_list

@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    search_fields = ["name"]

@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = ["email", "is_active", "confirmed_at", "unsubscribed_at", "bounce_count"]
    list_filter  = ["is_active", "country", "tags"]
    search_fields= ["email", "first_name", "last_name"]
    actions = ["mark_active", "mark_unsubscribed"]

    @admin.action(description="Mark selected subscribers as active")
    def mark_active(self, request, qs):
        updated = qs.update(is_active=True, confirmed_at=timezone.now())
        self.message_user(request, f"Activated {updated} subscribers.", messages.SUCCESS)

    @admin.action(description="Mark selected subscribers as unsubscribed")
    def mark_unsubscribed(self, request, qs):
        updated = 0
        for sub in qs:
            sub.mark_unsubscribed()
            updated += 1
        self.message_user(request, f"Unsubscribed {updated} subscribers.", messages.WARNING)

@admin.register(NewsletterTemplate)
class NewsletterTemplateAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name", "subject_template"]

@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display  = ["name", "status", "scheduled_at", "started_at", "finished_at"]
    list_filter   = ["status", "scheduled_at"]
    filter_horizontal = ["include_tags", "exclude_tags"]
    actions = ["build_recipients", "schedule_now", "send_now", "cancel_campaign"]

    @admin.action(description="Build recipient list (snapshot) for selected campaigns")
    def build_recipients(self, request, qs):
        count = 0
        for campaign in qs:
            added = build_recipient_list(campaign.id)
            count += added
        self.message_user(request, f"Built {count} recipient rows.", messages.SUCCESS)

    @admin.action(description="Schedule selected campaigns to run now")
    def schedule_now(self, request, qs):
        now = timezone.now()
        updated = qs.update(scheduled_at=now, status="scheduled")
        self.message_user(request, f"Scheduled {updated} campaigns.", messages.SUCCESS)

    @admin.action(description="Send selected campaigns now (enqueue Celery)")
    def send_now(self, request, qs):
        enq = 0
        for c in qs:
            enqueue_campaign_send.delay(c.id)  # Celery async
            enq += 1
        self.message_user(request, f"Enqueued {enq} campaigns for sending.", messages.SUCCESS)

    @admin.action(description="Cancel selected campaigns")
    def cancel_campaign(self, request, qs):
        updated = qs.filter(status__in=["draft","scheduled","sending"]).update(status="cancelled")
        self.message_user(request, f"Cancelled {updated} campaigns.", messages.WARNING)

@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    list_display = ["campaign", "subscriber", "sent_at", "delivered", "opened", "bounced"]
    list_filter  = ["campaign", "delivered", "opened", "bounced"]
    search_fields= ["subscriber__email", "campaign__name"]

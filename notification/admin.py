# notification/admin.py
from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from .models import Notification, ContactInquiry, SupportTicket, TicketReply


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = [
        "colored_verb",
        "recipient_link",
        "actor_link",
        "target_link",
        "is_read",
        "created_at_formatted",
    ]
    list_filter = ["verb", "is_read", "created_at", "recipient"]
    search_fields = [
        "recipient__email",
        "recipient__username",
        "data__order_number",
        "data__message",
    ]
    readonly_fields = ["created_at", "recipient", "verb", "actor", "target", "data_display"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]
    list_per_page = 50

    def has_add_permission(self, request):
        # Optional: disable manual creation (notifications should be created programmatically)
        return False

    def has_change_permission(self, request, obj=None):
        return True  # allow marking as read

    def has_delete_permission(self, request, obj=None):
        return True

    # ──────────────────────────────────────
    # Pretty columns
    # ──────────────────────────────────────
    def colored_verb(self, obj):
        colors = {
            "vendor_new_order": "bg-green-100 text-green-800",
            "vendor_payout": "bg-blue-100 text-blue-800",
            "customer_order_placed": "bg-purple-100 text-purple-800",
            "announcement": "bg-yellow-100 text-yellow-800",
        }
        color = colors.get(obj.verb, "bg-gray-100 text-gray-800")
        return format_html(
            '<span class="px-2 py-1 rounded text-xs font-medium {}">{}</span>',
            color,
            obj.get_verb_display(),
        )
    colored_verb.short_description = "Type"

    def recipient_link(self, obj):
        url = reverse("admin:userauths_user_change", args=[obj.recipient.id])
        return format_html('<a href="{}">{}</a>', url, obj.recipient)
    recipient_link.short_description = "Recipient"

    def actor_link(self, obj):
        if not obj.actor:
            return "—"
        ct = ContentType.objects.get_for_model(obj.actor)
        url = reverse(f"admin:{ct.app_label}_{ct.model}_change", args=[obj.actor.id])
        return format_html('<a href="{}">{}</a>', url, str(obj.actor))
    actor_link.short_description = "Actor"

    def target_link(self, obj):
        if not obj.target:
            return "—"
        ct = ContentType.objects.get_for_model(obj.target)
        url = reverse(f"admin:{ct.app_label}_{ct.model}_change", args=[obj.target.id])
        return format_html('<a href="{}">{}</a>', url, str(obj.target))
    target_link.short_description = "Target"

    def created_at_formatted(self, obj):
        return obj.created_at.strftime("%b %d, %Y %I:%M %p")
    created_at_formatted.short_description = "Created"

    # ──────────────────────────────────────
    # Pretty detail view
    # ──────────────────────────────────────
    def data_display(self, obj):
        if not obj.data:
            return "—"
        items = []
        for key, value in obj.data.items():
            if key == "order_number":
                items.append(f'<strong>Order #:</strong> {value}')
            elif key == "amount":
                items.append(f'<strong>Amount:</strong> ${value}')
            elif key == "url":
                items.append(f'<a href="{value}" target="_blank">View →</a>')
            else:
                items.append(f'<strong>{key}:</strong> {value}')
        return format_html("<br>".join(items))
    data_display.short_description = "Extra Data"

    # ──────────────────────────────────────
    # Inline actions
    # ──────────────────────────────────────
    actions = ["mark_as_read", "mark_as_unread"]

    def mark_as_read(self, request, queryset):
        queryset.update(is_read=True)
        self.message_user(request, f"{queryset.count()} notification(s) marked as read.")
    mark_as_read.short_description = "Mark selected as read"

    def mark_as_unread(self, request, queryset):
        queryset.update(is_read=False)
        self.message_user(request, f"{queryset.count()} notification(s) marked as unread.")
    mark_as_unread.short_description = "Mark selected as unread"


# 3. The inline (this stays exactly the same)
class TicketReplyInline(admin.TabularInline):
    model = TicketReply
    extra = 1
    fields = ['message', 'is_internal', 'replied_by', 'created_at']
    readonly_fields = ['replied_by', 'created_at']

@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ['ticket_id', 'inquiry', 'priority', 'assigned_to', 'get_status', 'get_email']
    list_filter = ['priority', 'inquiry__status', 'assigned_to']
    search_fields = ['ticket_id', 'inquiry__email', 'inquiry__subject']
    readonly_fields = ['ticket_id']

    # Show replies inline under each ticket
    inlines = [TicketReplyInline]

    def get_status(self, obj):
        return obj.inquiry.get_status_display()
    get_status.short_description = "Status"

    def get_email(self, obj):
        return obj.inquiry.email
    get_email.short_description = "Customer Email"


# 2. Nice view for ContactInquiry (optional, but clean)
@admin.register(ContactInquiry)
class ContactInquiryAdmin(admin.ModelAdmin):
    list_display = ['get_ticket_id', 'name', 'email', 'inquiry_type', 'status', 'created_at']
    list_filter = ['status', 'inquiry_type', 'created_at']
    search_fields = ['name', 'email', 'subject']
    readonly_fields = ['created_at', 'updated_at', 'replied_at']

    def get_ticket_id(self, obj):
        return obj.support_ticket.ticket_id if hasattr(obj, 'support_ticket') else '-'
    get_ticket_id.short_description = "Ticket ID"

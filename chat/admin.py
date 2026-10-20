from django.contrib import admin

from .models import ChatViolation, Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    can_delete = False
    fields = ['created_at', 'sender_side', 'sender', 'body']
    readonly_fields = fields
    ordering = ['-id']
    show_change_link = False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('sender')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    """Read-only: staff look into disputes and reports; they don't post as either side."""
    list_display = ['id', 'customer', 'vendor', 'reason', 'last_message_at', 'customer_unread', 'vendor_unread']
    list_filter = ['reason']
    search_fields = ['customer__email', 'customer__first_name', 'vendor__name']
    raw_id_fields = ['customer', 'vendor', 'product']
    readonly_fields = [f.name for f in Conversation._meta.fields]
    inlines = [MessageInline]
    date_hierarchy = 'last_message_at'

    def has_add_permission(self, request):
        return False


@admin.register(ChatViolation)
class ChatViolationAdmin(admin.ModelAdmin):
    """Messages refused for trying to move a deal off Negromart."""
    list_display = ['created_at', 'user', 'side', 'categories', 'conversation', 'short_excerpt']
    list_filter = ['side', 'created_at']
    search_fields = ['user__email', 'excerpt']
    raw_id_fields = ['user', 'conversation']
    readonly_fields = ['user', 'conversation', 'side', 'categories', 'excerpt', 'created_at']

    def has_add_permission(self, request):
        return False

    @admin.display(description='Message')
    def short_excerpt(self, obj):
        return obj.excerpt[:80]

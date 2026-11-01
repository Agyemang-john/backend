from django.contrib import admin
from django.utils.html import format_html_join

from .models import AuditEntry


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    """Read-only, append-only."""
    list_display = ('created_at', 'actor_label', 'via', 'action', 'model', 'object_id', 'object_label', 'summary')
    list_filter = ('action', 'via', 'model')
    search_fields = ('actor_label', 'object_id', 'object_label', 'request_id')
    date_hierarchy = 'created_at'
    readonly_fields = [f.name for f in AuditEntry._meta.fields] + ['changes_table']
    exclude = ('changes',)

    @admin.display(description='Changes')
    def summary(self, obj):
        return ', '.join(list(obj.changes)[:4])

    @admin.display(description='Changes')
    def changes_table(self, obj):
        return format_html_join('', '<div><b>{}</b>: {} → {}</div>', ((k, v[0], v[1]) for k, v in obj.changes.items()))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

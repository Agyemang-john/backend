from django.contrib import admin, messages
from django.utils.html import format_html_join

from . import services
from .models import OrderRisk


@admin.register(OrderRisk)
class OrderRiskAdmin(admin.ModelAdmin):
    list_display = ('order', 'score', 'level', 'status', 'why', 'created_at', 'reviewed_by')
    list_filter = ('level', 'status')
    search_fields = ('order__order_number', 'order__user__email')
    raw_id_fields = ('order', 'reviewed_by')
    readonly_fields = ('order', 'score', 'level', 'reasons', 'created_at', 'reviewed_by', 'reviewed_at')
    actions = ['approve', 'cancel_as_fraud']
    ordering = ('-created_at',)

    @admin.display(description='Why')
    def why(self, obj):
        return format_html_join(', ', '{} (+{})', ((r['label'], r['points']) for r in obj.reasons))

    @admin.action(description='Approve: let sellers ship')
    def approve(self, request, queryset):
        for risk in queryset:
            try:
                services.approve(risk, request.user)
            except services.RiskError as exc:
                self.message_user(request, f"{risk.order_id}: {exc}", messages.ERROR)

    @admin.action(description='Cancel as fraud (restock, refund if prepaid)')
    def cancel_as_fraud(self, request, queryset):
        for risk in queryset:
            try:
                services.cancel_as_fraud(risk, request.user)
            except services.RiskError as exc:
                self.message_user(request, f"{risk.order_id}: {exc}", messages.ERROR)

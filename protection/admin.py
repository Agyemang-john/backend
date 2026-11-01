from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm
from django.utils import timezone

from . import services
from .models import Claim, ClaimMessage


class DecisionForm(ActionForm):
    amount = forms.DecimalField(required=False, max_digits=10, decimal_places=2,
                                help_text="Refund amount (blank = amount claimed)")
    note = forms.CharField(required=False, widget=forms.TextInput(attrs={'size': 60}),
                           help_text="Explanation shown to both sides")


class MessageInline(admin.TabularInline):
    model = ClaimMessage
    extra = 1
    fields = ('side', 'author', 'text', 'image', 'created_at')
    readonly_fields = ('created_at',)
    raw_id_fields = ('author',)


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ('reference', 'status', 'reason', 'amount_claimed', 'vendor', 'buyer', 'seller_respond_by',
                    'escalated_at', 'resolved_at', 'refund_sent_at')
    list_filter = ('status', 'reason', ('refund_sent_at', admin.EmptyFieldListFilter))
    search_fields = ('reference', 'order__order_number', 'vendor__name', 'buyer__email')
    raw_id_fields = ('order', 'order_product', 'vendor', 'buyer', 'resolved_by')
    readonly_fields = ('reference', 'created_at', 'updated_at')
    inlines = [MessageInline]
    action_form = DecisionForm
    actions = ['refund_buyer', 'decide_for_seller', 'mark_refund_sent']
    ordering = ('status', 'escalated_at')

    @admin.action(description="Decide for the shopper: refund (amount below, else full claim)")
    def refund_buyer(self, request, queryset):
        amount = request.POST.get('amount')
        note = request.POST.get('note') or 'Negromart decided in the shopper’s favour.'
        done = 0
        for claim in queryset:
            try:
                services.resolve_for_buyer(claim, Decimal(amount) if amount else claim.amount_claimed, request.user, note)
                done += 1
            except services.ClaimError as exc:
                self.message_user(request, f"{claim.reference}: {exc}", messages.ERROR)
        self.message_user(request, f"Resolved {done} claim(s) for the shopper. Send the refunds, then mark them sent.")

    @admin.action(description="Decide for the seller (note required)")
    def decide_for_seller(self, request, queryset):
        note = request.POST.get('note') or ''
        for claim in queryset:
            try:
                services.resolve_for_seller(claim, request.user, note)
            except services.ClaimError as exc:
                self.message_user(request, f"{claim.reference}: {exc}", messages.ERROR)

    @admin.action(description="Mark refund sent to the shopper")
    def mark_refund_sent(self, request, queryset):
        n = queryset.filter(status=Claim.RESOLVED_BUYER, refund_sent_at__isnull=True).update(refund_sent_at=timezone.now())
        self.message_user(request, f"Marked {n} refund(s) as sent.")

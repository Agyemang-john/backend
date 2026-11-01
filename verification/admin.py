from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join

from . import services
from .models import KycEvent, VerificationProfile

STATE_CHOICES = [('', '— no decision —'), ('accepted', 'Accept'), ('rejected', 'Reject')]


class ReviewForm(forms.ModelForm):
    """One decision + reason per document, applied through services.review on save."""

    class Meta:
        model = VerificationProfile
        fields = ('id_type', 'id_number', 'business_reg_number', 'review_note')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field, label in services.DOC_LABELS.items():
            current = (self.instance.documents or {}).get(field, {})
            self.fields[f'{field}__state'] = forms.ChoiceField(choices=STATE_CHOICES, required=False, label=f"{label}: decision",
                                                               initial=current.get('state') if current.get('state') in ('accepted', 'rejected') else '')
            self.fields[f'{field}__reason'] = forms.CharField(required=False, label=f"{label}: reason if rejected",
                                                              initial=current.get('reason', ''), max_length=300)


class EventInline(admin.TabularInline):
    model = KycEvent
    fk_name = 'vendor'
    extra = 0
    can_delete = False
    readonly_fields = ('created_at', 'kind', 'detail', 'actor')
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(VerificationProfile)
class VerificationProfileAdmin(admin.ModelAdmin):
    form = ReviewForm
    list_display = ('vendor', 'vendor_status', 'id_type', 'submitted_at', 'reviewed_at', 'needs_recheck', 'payout_match')
    list_filter = ('needs_recheck', 'vendor__status', 'id_type')
    search_fields = ('vendor__name', 'id_number', 'business_reg_number')
    readonly_fields = ('vendor', 'documents_preview', 'payout_match', 'history', 'submitted_at', 'reviewed_at', 'reviewed_by')
    ordering = ('-needs_recheck', '-submitted_at')

    def has_add_permission(self, request):
        # Profiles are created when a seller submits documents; there's nothing to review on a blank one.
        return False

    def get_form(self, request, obj=None, **kwargs):
        # The fieldsets list the per-document decision fields, which ReviewForm adds itself.
        # Left to the admin, they'd be handed to modelform_factory as model fields -> FieldError (500).
        kwargs['fields'] = ReviewForm.Meta.fields
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        decisions = []
        for field in services.DOC_LABELS:
            decisions += [f'{field}__state', f'{field}__reason']
        return [
            (None, {'fields': ('vendor', 'documents_preview', 'payout_match')}),
            ('Identity', {'fields': ('id_type', 'id_number', 'business_reg_number')}),
            ('Decision', {'fields': decisions + ['review_note'],
                          'description': 'Accepting every required document approves the store; rejecting any sends '
                                         'the reasons to the seller to fix.'}),
            ('History', {'fields': ('history', 'submitted_at', 'reviewed_at', 'reviewed_by')}),
        ]

    @admin.display(description='Store status')
    def vendor_status(self, obj):
        return obj.vendor.status

    @admin.display(description='Payout name match')
    def payout_match(self, obj):
        score = services.payout_name_match(obj.vendor)
        if score is None:
            return '—'
        colour = '#2E7D32' if score >= 80 else '#E65100' if score >= 50 else '#C62828'
        return format_html('<b style="color:{}">{}%</b>', colour, score)

    @admin.display(description='Documents (links expire in 10 minutes)')
    def documents_preview(self, obj):
        v = obj.vendor
        rows = []
        for field in services.required_documents(v):
            f = getattr(v, field)
            state = (obj.documents or {}).get(field, {}).get('state', 'pending' if f else 'missing')
            link = format_html('<a href="{}" target="_blank" rel="noopener">Open</a>', f.url) if f else 'not uploaded'
            rows.append((services.DOC_LABELS[field], state, link))
        return format_html('<table>{}</table>', format_html_join('', '<tr><td>{}</td><td>{}</td><td>{}</td></tr>', rows))

    @admin.display(description='History')
    def history(self, obj):
        events = KycEvent.objects.filter(vendor=obj.vendor)[:30]
        return format_html_join('', '<div>{} · <b>{}</b> · {}</div>',
                                ((e.created_at.strftime('%d %b %Y %H:%M'), e.kind, e.detail) for e in events)) or '—'

    def save_model(self, request, obj, form, change):
        obj.save()
        decisions = {}
        for field in services.DOC_LABELS:
            state = form.cleaned_data.get(f'{field}__state')
            if state:
                decisions[field] = (state, form.cleaned_data.get(f'{field}__reason', ''))
        if decisions:
            try:
                outcome = services.review(obj.vendor, request.user, decisions, form.cleaned_data.get('review_note', ''))
                self.message_user(request, f"Review saved: store {outcome}.")
            except services.KycError as exc:
                self.message_user(request, str(exc), messages.ERROR)

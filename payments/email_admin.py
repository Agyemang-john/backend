# subscriptions/email_admin.py
#
# Add to your admin.py (whichever app — payments or subscriptions):
#   from .email_admin import EmailTemplateAdmin, SubscriptionEmailConfigAdmin

from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse, path
from django.http import HttpResponseRedirect
from django.contrib import messages
from django.utils.safestring import mark_safe

from .email_models import EmailTemplate, SubscriptionEmailConfig


def _admin_url(model_class, action, *args):
    """
    Build an admin URL using the model's actual app_label at runtime.
    This avoids hardcoding 'subscriptions_' or 'payments_' which causes
    NoReverseMatch when the Django app_name differs from the module name.
    """
    app   = model_class._meta.app_label
    model = model_class._meta.model_name
    return reverse(f'admin:{app}_{model}_{action}', args=args or None)


# ─────────────────────────────────────────────────────────────────────────────
# Email Template admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display    = ('type_badge', 'subject', 'html_file', 'is_active', 'updated_at', 'send_test_button')
    list_filter     = ('is_active',)
    search_fields   = ('type', 'subject')
    readonly_fields = ('created_at', 'updated_at', 'available_variables')
    ordering        = ('type',)

    fieldsets = (
        ('Template Identity', {
            'fields': ('type', 'is_active'),
        }),
        ('Subject Line', {
            'fields': ('subject',),
            'description': 'Supports variables like {{ vendor_name }}, {{ plan_name }}.',
        }),
        ('HTML Template File', {
            'fields': ('html_file',),
            'description': (
                'Path inside your templates/ directory, e.g. '
                '<code>emails/subscription_confirmation.html</code>. '
                'If the file is found it will be used; otherwise the plain-text fallback is sent.'
            ),
        }),
        ('Plain-text Fallback', {
            'fields': ('text_body',),
            'classes': ('collapse',),
        }),
        ('Available Template Variables', {
            'fields': ('available_variables',),
            'classes': ('collapse',),
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── Custom URL for the "Send Test" button ─────────────────────────────────
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                '<int:template_id>/send-test/',
                self.admin_site.admin_view(self.send_test_email_view),
                # Name includes app_label so it's unique and reversible
                name=f'{EmailTemplate._meta.app_label}_emailtemplate_send_test',
            ),
        ]
        return custom + urls

    def send_test_button(self, obj):
        if not obj.pk:
            return '—'
        url_name = f'admin:{EmailTemplate._meta.app_label}_emailtemplate_send_test'
        try:
            url = reverse(url_name, args=[obj.pk])
        except Exception:
            # Fallback to a direct path
            url = f'../{obj.pk}/send-test/'
        return format_html('<a href="{}" class="button">Send Test</a>', url)
    send_test_button.short_description = 'Test'

    def send_test_email_view(self, request, template_id):
        from .email_tasks import send_templated_email
        from django.conf import settings

        template = EmailTemplate.objects.get(pk=template_id)
        test_context = {
            'vendor_name':  request.user.get_full_name() or request.user.username,
            'plan_name':    'Pro',
            'amount':       'GHS 150.00',
            'end_date':     '31 December 2025',
            'days_left':    7,
            'frontend_url': getattr(settings, 'FRONTEND_URL', 'https://seller.negromart.com'),
            'billing_url':  getattr(settings, 'FRONTEND_URL', 'https://seller.negromart.com') + '/billing/cards/',
            'support_url':  getattr(settings, 'FRONTEND_URL', 'https://seller.negromart.com') + '/support/',
        }
        try:
            send_templated_email(
                template_type=template.type,
                recipient_email=request.user.email,
                context=test_context,
            )
            self.message_user(request, f'Test email sent to {request.user.email}', messages.SUCCESS)
        except Exception as e:
            self.message_user(request, f'Failed to send test: {e}', messages.ERROR)

        return HttpResponseRedirect(_admin_url(EmailTemplate, 'changelist'))

    # ── Display helpers ───────────────────────────────────────────────────────

    def type_badge(self, obj):
        colours = {
            'confirmation':    '#16a34a',
            'renewal_success': '#2563eb',
            'expiring_soon':   '#d97706',
            'expired':         '#dc2626',
            'payment_failed':  '#9333ea',
            'cancellation':    '#64748b',
        }
        colour = colours.get(obj.type, '#6b7280')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 10px;border-radius:12px;'
            'font-size:11px;font-weight:700">{}</span>',
            colour, obj.get_type_display()
        )
    type_badge.short_description = 'Type'

    def available_variables(self, obj):
        vars_map = {
            'confirmation':    ['vendor_name', 'plan_name', 'end_date', 'amount', 'frontend_url', 'support_url'],
            'renewal_success': ['vendor_name', 'plan_name', 'amount', 'end_date', 'frontend_url'],
            'expiring_soon':   ['vendor_name', 'plan_name', 'end_date', 'days_left', 'frontend_url', 'subscribe_url'],
            'expired':         ['vendor_name', 'frontend_url', 'support_url', 'subscribe_url'],
            'payment_failed':  ['vendor_name', 'frontend_url', 'billing_url'],
            'cancellation':    ['vendor_name', 'plan_name', 'end_date', 'frontend_url', 'subscribe_url'],
        }
        variables = vars_map.get(obj.type, [])
        if not variables:
            return '—'
        items = ''.join(
            f'<code style="background:#f1f5f9;padding:2px 8px;border-radius:4px;'
            f'margin:2px;display:inline-block">{{{{ {v} }}}}</code>'
            for v in variables
        )
        return mark_safe(f'<div style="line-height:2.4">{items}</div>')
    available_variables.short_description = 'Variables you can use in subject / HTML'


# ─────────────────────────────────────────────────────────────────────────────
# Schedule & Email Config admin (singleton)
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SubscriptionEmailConfig)
class SubscriptionEmailConfigAdmin(admin.ModelAdmin):

    def has_add_permission(self, request):
        # Only allow adding if the singleton row doesn't exist yet
        return not SubscriptionEmailConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    list_display    = ('__str__', 'expiry_warning_days', 'second_warning_days', 'renewal_advance_days', 'from_email')
    readonly_fields = ('schedule_summary', 'celery_beat_note')

    fieldsets = (
        ('⏰ Celery Beat Schedule (UTC)', {
            'fields': ('celery_beat_note',),
        }),
        ('Renewal Task', {
            'fields': (('run_renewals_hour', 'run_renewals_minute'),),
            'description': 'Charge vendor cards. Runs daily at the time you set.',
        }),
        ('Expiry Warning Task', {
            'fields': (('run_expiry_check_hour', 'run_expiry_check_minute'),),
            'description': 'Send "expiring soon" emails. Runs daily.',
        }),
        ('Expiration Cleanup Task', {
            'fields': (('run_expire_old_hour', 'run_expire_old_minute'),),
            'description': 'Mark past-end-date subscriptions as expired.',
        }),
        ('📅 Warning Thresholds', {
            'fields': ('expiry_warning_days', 'second_warning_days', 'renewal_advance_days', 'renewal_max_retries'),
            'description': (
                '<strong>expiry_warning_days</strong>: Days before expiry for the first warning email.<br>'
                '<strong>second_warning_days</strong>: Days before expiry for the second warning (0 = disabled).<br>'
                '<strong>renewal_advance_days</strong>: Days before expiry to attempt the auto-renewal charge.'
            ),
        }),
        ('📧 Email Sender Settings', {
            'fields': ('from_email', 'from_name', 'reply_to', 'frontend_url', 'support_url'),
        }),
        ('📊 Current Schedule', {
            'fields': ('schedule_summary',),
            'classes': ('collapse',),
        }),
    )

    def celery_beat_note(self, obj):
        return mark_safe(
            '<div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;'
            'padding:12px 16px;margin-bottom:4px">'
            '<strong>⚡ Live schedule control</strong> — Saving this form automatically updates '
            'the django-celery-beat PeriodicTask entries. Beat picks up the new schedule within '
            '5 minutes. <strong>No worker restart needed.</strong><br><br>'
            'Times are in <strong>UTC</strong>. Ghana is UTC+0 (no offset needed).'
            '</div>'
        )
    celery_beat_note.short_description = ''

    def schedule_summary(self, obj):
        if not obj:
            return '—'
        def fmt(h, m): return f'{int(h):02d}:{int(m):02d} UTC'
        rows = [
            ('Renewal charges',    fmt(obj.run_renewals_hour,     obj.run_renewals_minute),     f'{obj.renewal_advance_days} day(s) before expiry'),
            ('Expiry warnings',    fmt(obj.run_expiry_check_hour, obj.run_expiry_check_minute),  f'{obj.expiry_warning_days} days + {obj.second_warning_days} days before'),
            ('Expiration cleanup', fmt(obj.run_expire_old_hour,   obj.run_expire_old_minute),    'Marks expired subscriptions'),
        ]
        table_rows = ''.join(
            f'<tr><td style="padding:6px 14px;font-weight:600;border-bottom:1px solid #f1f5f9">{r[0]}</td>'
            f'<td style="padding:6px 14px;color:#2563eb;font-weight:700;border-bottom:1px solid #f1f5f9">{r[1]}</td>'
            f'<td style="padding:6px 14px;color:#64748b;border-bottom:1px solid #f1f5f9">{r[2]}</td></tr>'
            for r in rows
        )
        return mark_safe(
            '<table style="border-collapse:collapse;font-size:13px;width:100%">'
            '<thead><tr style="background:#f8fafc">'
            '<th style="padding:8px 14px;text-align:left;color:#64748b;font-size:11px;letter-spacing:0.06em;text-transform:uppercase">Task</th>'
            '<th style="padding:8px 14px;text-align:left;color:#64748b;font-size:11px;letter-spacing:0.06em;text-transform:uppercase">Runs at</th>'
            '<th style="padding:8px 14px;text-align:left;color:#64748b;font-size:11px;letter-spacing:0.06em;text-transform:uppercase">Notes</th>'
            f'</tr></thead><tbody>{table_rows}</tbody></table>'
        )
    schedule_summary.short_description = 'Current Schedule'

    def changelist_view(self, request, extra_context=None):
        """
        Bypass the list page and go straight to the singleton edit form.
        Uses _meta.app_label so it works whether the app is 'payments' or 'subscriptions'.
        """
        # Ensure the singleton row exists
        SubscriptionEmailConfig.get()
        # Build the URL dynamically — avoids hardcoding the app label
        try:
            url = _admin_url(SubscriptionEmailConfig, 'change', 1)
            return HttpResponseRedirect(url)
        except Exception:
            # If the URL can't be built yet (e.g. during first migration), fall back to default
            return super().changelist_view(request, extra_context)

    def get_object(self, request, object_id, from_field=None):
        # Always return the singleton row regardless of what object_id was passed
        return SubscriptionEmailConfig.get()
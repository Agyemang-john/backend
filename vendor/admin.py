from django.contrib import admin
from vendor.models import *
from .tasks import send_vendor_approval_email, send_vendor_sms


class VendorActivityLogInline(admin.TabularInline):
    model = VendorActivityLog
    extra = 0
    readonly_fields = ('event_type', 'ip_address', 'user_agent', 'metadata', 'created_at')
    can_delete = False
    max_num = 20
    ordering = ('-created_at',)


class VendorAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'email', 'contact', 'status', 'is_approved', 'is_suspended',
        'is_subscribed', 'subscription_end_date', 'is_featured',
        'inactivity_auto_closed', 'last_seen_at', 'last_login_at',
    )
    list_editable = ('is_featured', 'is_approved', 'is_suspended',)
    list_filter = (
        'status', 'is_approved', 'is_suspended', 'vendor_type', 'country',
        'inactivity_auto_closed',
    )
    search_fields = ('name', 'email', 'contact')
    inlines = [VendorActivityLogInline]

    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'user', 'email', 'contact', 'country', 'vendor_type', 'business_type', 'shipping_from_country')
        }),
        ('Documents', {
            'fields': ('license', 'student_id', 'proof_of_address', 'government_issued_id')
        }),
        ('Status', {
            'fields': ('status', 'is_approved', 'is_suspended', 'is_subscribed', 'subscription_start_date', 'subscription_end_date')
        }),
        ('Analytics', {
            'fields': ('followers', 'is_featured', 'is_manufacturer', 'views')
        }),
        ('Shop Status', {
            'fields': ('shop_paused', 'shop_paused_at'),
        }),
        ('Activity Tracking', {
            'fields': (
                'last_login_at', 'last_seen_at', 'last_logout_at',
                'total_login_count', 'inactivity_auto_closed', 'inactivity_closed_at',
            ),
            'classes': ('collapse',),
        }),
    )

    readonly_fields = (
        'last_login_at', 'last_seen_at', 'last_logout_at',
        'total_login_count', 'inactivity_closed_at', 'shop_paused_at',
    )

    actions = ['approve_vendors', 'reject_vendors', 'suspend_vendors', 'reopen_inactive_shops']

    def approve_vendors(self, request, queryset):
        """Approve selected vendors and send notifications."""
        for vendor in queryset:
            if vendor.status == 'PENDING':
                vendor.status = 'VERIFIED'
                vendor.is_approved = True
                vendor.is_suspended = False
                vendor.subscription_start_date = timezone.now().date()
                vendor.subscription_end_date = timezone.now().date() + timedelta(days=365)  # 1-year subscription
                vendor.is_subscribed = True
                vendor.user.role = 'vendor'
                vendor.save()
                logger.info(f"Vendor {vendor.name} approved by {request.user}")
                send_vendor_approval_email.delay(vendor.id, True)
                send_vendor_sms.delay(vendor.id, True)
        self.message_user(request, f"{queryset.count()} vendor(s) approved.")

    approve_vendors.short_description = "Approve selected vendors"

    def reject_vendors(self, request, queryset):
        """Reject selected vendors and send notifications."""
        for vendor in queryset:
            if vendor.status == 'PENDING':
                vendor.status = 'REJECTED'
                vendor.is_approved = False
                vendor.is_suspended = False
                vendor.user.role = 'customer'
                vendor.save()
                logger.info(f"Vendor {vendor.name} rejected by {request.user}")
                send_vendor_approval_email.delay(vendor.id, False)
                send_vendor_sms.delay(vendor.id, False)
        self.message_user(request, f"{queryset.count()} vendor(s) rejected.")

    reject_vendors.short_description = "Reject selected vendors"

    def suspend_vendors(self, request, queryset):
        """Suspend selected vendors."""
        for vendor in queryset:
            if vendor.status == 'VERIFIED':
                vendor.status = 'SUSPENDED'
                vendor.is_suspended = True
                vendor.is_approved = False
                vendor.user.role = 'customer'
                vendor.save()
                logger.info(f"Vendor {vendor.name} suspended by {request.user}")
                send_vendor_approval_email.delay(vendor.id, False)
                send_vendor_sms.delay(vendor.id, False)
        self.message_user(request, f"{queryset.count()} vendor(s) suspended.")

    suspend_vendors.short_description = "Suspend selected vendors"

    def reopen_inactive_shops(self, request, queryset):
        """Manually reopen shops that were auto-closed due to inactivity."""
        from .models import VendorActivityLog
        from django.utils import timezone as tz
        count = 0
        for vendor in queryset.filter(inactivity_auto_closed=True):
            vendor.inactivity_auto_closed = False
            vendor.inactivity_closed_at = None
            vendor.save(update_fields=['inactivity_auto_closed', 'inactivity_closed_at', 'modified_at'])
            VendorActivityLog.objects.create(
                vendor=vendor,
                event_type='manual_reopen',
                metadata={'reopened_by': str(request.user)},
            )
            count += 1
        self.message_user(request, f"{count} shop(s) reopened.")

    reopen_inactive_shops.short_description = "Reopen auto-closed shops (inactivity)"

    def get_fields(self, request, obj=None):
        # super() may return a tuple which is immutable — convert to list
        fields = list(super().get_fields(request, obj))
        if obj:
            # Show only the relevant ID field based on vendor type
            if obj.vendor_type == 'student' and 'government_issued_id' in fields:
                fields.remove('government_issued_id')
            elif 'student_id' in fields:
                fields.remove('student_id')
        return fields

class VendorProfileAdmin(admin.ModelAdmin):
    list_display = '_all_'

class OpeningHourAdmin(admin.ModelAdmin):
    list_display = ('vendor', 'day', 'from_hour', 'to_hour', 'is_closed')
    list_filter = ('is_closed', 'day')


@admin.register(VendorActivityLog)
class VendorActivityLogAdmin(admin.ModelAdmin):
    list_display  = ('vendor', 'event_type', 'ip_address', 'created_at')
    list_filter   = ('event_type',)
    search_fields = ('vendor__name', 'ip_address')
    readonly_fields = ('vendor', 'event_type', 'ip_address', 'user_agent', 'metadata', 'created_at')
    ordering      = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(Vendor, VendorAdmin)
admin.site.register(About)
admin.site.register(VendorPaymentMethod)
admin.site.register(OpeningHour, OpeningHourAdmin)

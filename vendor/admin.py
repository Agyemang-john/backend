from django.contrib import admin
from vendor.models import *
from .tasks import send_vendor_approval_email, send_vendor_sms


class VendorAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'contact', 'status', 'is_approved', 'is_suspended', 'is_subscribed', 'subscription_end_date', 'is_featured')
    list_editable = ('is_featured', 'is_approved', 'is_suspended',)
    list_filter = ('status', 'is_approved', 'is_suspended', 'vendor_type', 'country')
    search_fields = ('name', 'email', 'contact')

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
    )   

    actions = ['approve_vendors', 'reject_vendors', 'suspend_vendors']

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

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj:
            if obj.vendor_type == 'student':
                fields.remove('government_issued_id')
            else:
                fields.remove('student_id')
        return fields

class VendorProfileAdmin(admin.ModelAdmin):
    list_display = '_all_'

class OpeningHourAdmin(admin.ModelAdmin):
    list_display = ('vendor', 'day', 'from_hour', 'to_hour', 'is_closed')
    list_filter = ('is_closed', 'day')


admin.site.register(Vendor, VendorAdmin)
admin.site.register(About)
admin.site.register(VendorPaymentMethod)
admin.site.register(OpeningHour, OpeningHourAdmin)

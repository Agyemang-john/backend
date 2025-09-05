from django.contrib import admin
from vendor.models import *


class VendorAdmin(admin.ModelAdmin):
    prepopulated_fields = {'slug': ('name',)}
    list_display = ('user', 'name', 'is_featured', 'is_approved', 'created_at')
    list_editable = ('is_featured', 'is_approved',)

    list_filter = ('vendor_type', 'is_approved', 'name',)

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj:
            if obj.vendor_type == 'student':
                fields.remove('license')
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

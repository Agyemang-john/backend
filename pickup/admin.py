from django.contrib import admin

from .models import PickupParcel, PickupPoint, PickupPointStaff


class StaffInline(admin.TabularInline):
    model = PickupPointStaff
    raw_id_fields = ('user',)
    extra = 1


@admin.register(PickupPoint)
class PickupPointAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'town', 'region', 'partner', 'is_active', 'hold_days')
    list_filter = ('is_active', 'region')
    search_fields = ('name', 'town', 'code')
    prepopulated_fields = {'code': ('name',)}
    inlines = [StaffInline]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from django.core.cache import cache
        cache.delete('pickup:points')


@admin.register(PickupParcel)
class PickupParcelAdmin(admin.ModelAdmin):
    list_display = ('order', 'point', 'status', 'ready_at', 'expires_at', 'collected_at')
    list_filter = ('status', 'point')
    search_fields = ('order__order_number', 'code')
    raw_id_fields = ('order', 'point', 'collected_by_staff')
    readonly_fields = ('code',)

from django.contrib import admin, messages
import logging

from .models import Address, Country, Region, Town, Location
from .tasks import seed_countries

logger = logging.getLogger(__name__)


class ArchivedFilter(admin.SimpleListFilter):
    title = 'in address book'
    parameter_name = 'archived'

    def lookups(self, request, model_admin):
        return (('no', 'Active'), ('yes', 'Archived (kept for orders)'))

    def queryset(self, request, queryset):
        if self.value() == 'no':
            return queryset.filter(archived_at__isnull=True)
        if self.value() == 'yes':
            return queryset.filter(archived_at__isnull=False)
        return queryset


class AddressAdmin(admin.ModelAdmin):
    list_display = ['user', 'label', 'address', 'town', 'country_code', 'location_source', 'status', 'archived_at']
    list_filter = [ArchivedFilter, 'location_source', 'label', 'country_code']
    search_fields = ['user__email', 'full_name', 'address', 'town', 'gps_address', 'mobile']
    raw_id_fields = ['user', 'replaced_by', 'pickup_point']
    readonly_fields = ['archived_at', 'replaced_by', 'last_used_at', 'date_added', 'modified_at']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')  # __str__ uses user.email


class LocationAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'region', 'town', 'user']
    list_select_related = ['country', 'region', 'town', 'user']
    raw_id_fields = ['user']

class CountryAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']
    actions = ['seed_countries']

    def seed_countries(self, request, queryset):
        """
        Admin action to trigger the Celery task for seeding countries.
        """
        task = seed_countries.delay()  # Run asynchronously
        self.message_user(
            request,
            f"Country seeding task has been queued (Task ID: {task.id}). Check Celery logs for progress.",
            messages.INFO
        )

    seed_countries.short_description = "Seed countries from pycountry"

admin.site.register(Address, AddressAdmin)
admin.site.register(Region)
admin.site.register(Country)
admin.site.register(Town)
admin.site.register(Location, LocationAdmin)

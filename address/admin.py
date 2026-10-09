from django.contrib import admin
from .models import Address, Country, Region, Town, Location
# Register your models here.
from .tasks import seed_countries
from django.contrib import messages
import logging
logger = logging.getLogger(__name__)

class AddressAdmin(admin.ModelAdmin):
    list_display = ['user', 'address', 'status']
    search_fields = ['user__email', 'full_name', 'address', 'town']
    raw_id_fields = ['user']

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

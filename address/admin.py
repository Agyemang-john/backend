from django.contrib import admin
from .models import Address, Country, Region, Town, Location
# Register your models here.
from .tasks import seed_countries
from django.contrib import messages
import logging
logger = logging.getLogger(__name__)

class AddressAdmin(admin.ModelAdmin):
    list_display = ['user', 'address', 'status']

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
admin.site.register(Location)

from django.contrib import admin
from .models import Address, Country, Region, Town, Location
# Register your models here.

class AddressAdmin(admin.ModelAdmin):
    list_display = ['user', 'address', 'status']

admin.site.register(Address, AddressAdmin)
admin.site.register(Region)
admin.site.register(Country)
admin.site.register(Town)
admin.site.register(Location)

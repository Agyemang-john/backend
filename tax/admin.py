from django.contrib import admin

from .models import SellerTaxRegistration, TaxExemption, TaxRate
from .services import clear_cache


class ClearsCache:
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        clear_cache()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        clear_cache()


@admin.register(TaxRate)
class TaxRateAdmin(ClearsCache, admin.ModelAdmin):
    list_display = ('country', 'code', 'name', 'rate', 'active', 'effective_from', 'position')
    list_filter = ('country', 'active')


@admin.register(TaxExemption)
class TaxExemptionAdmin(ClearsCache, admin.ModelAdmin):
    list_display = ('country', 'category', 'sub_category', 'reason')
    list_filter = ('country',)
    raw_id_fields = ('category', 'sub_category')


@admin.register(SellerTaxRegistration)
class SellerTaxRegistrationAdmin(admin.ModelAdmin):
    list_display = ('vendor', 'vat_registered', 'tax_id', 'verified', 'updated_at')
    list_filter = ('vat_registered', 'verified')
    search_fields = ('vendor__name', 'tax_id')
    raw_id_fields = ('vendor',)

from django.contrib import admin

from .models import AttributeDefinition, AttributeOption, ProductAttributeValue
from .services import clear_definitions_cache


class OptionInline(admin.TabularInline):
    model = AttributeOption
    extra = 3
    fields = ('value', 'slug', 'position')
    prepopulated_fields = {'slug': ('value',)}


@admin.register(AttributeDefinition)
class AttributeDefinitionAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'type', 'unit', 'filterable', 'required', 'position')
    list_filter = ('type', 'filterable', 'required')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ('categories', 'sub_categories')
    inlines = [OptionInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        clear_definitions_cache()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        clear_definitions_cache()


@admin.register(ProductAttributeValue)
class ProductAttributeValueAdmin(admin.ModelAdmin):
    list_display = ('product', 'definition', 'option', 'value_number', 'value_bool', 'value_text')
    list_filter = ('definition',)
    raw_id_fields = ('product', 'option')
    search_fields = ('product__title',)

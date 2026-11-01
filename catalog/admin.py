from django.contrib import admin, messages

from product.models import CatalogItem, Product

from . import services
from .buybox import recompute


@admin.action(description="Group as the same item (multi-seller)")
def group_as_same_item(modeladmin, request, queryset):
    try:
        item = services.group(queryset.select_related('vendor'))
    except services.CatalogError as exc:
        modeladmin.message_user(request, str(exc), messages.ERROR)
        return
    modeladmin.message_user(request, f"Grouped {queryset.count()} listings as “{item.title}”.", messages.SUCCESS)


@admin.action(description="Remove from multi-seller group")
def remove_from_group(modeladmin, request, queryset):
    for p in queryset.filter(catalog_item__isnull=False):
        services.ungroup(p)
    modeladmin.message_user(request, "Removed from their groups.", messages.SUCCESS)


# Add the actions to the existing Product admin (product/admin.py).
_product_admin = admin.site._registry.get(Product)
if _product_admin is not None:
    _product_admin.actions = list(_product_admin.actions or []) + [group_as_same_item, remove_from_group]


class OfferInline(admin.TabularInline):
    model = Product
    fk_name = 'catalog_item'
    fields = ('title', 'vendor', 'price', 'total_quantity', 'status', 'is_buybox_winner')
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.action(description="Recompute featured offer")
def recompute_selected(modeladmin, request, queryset):
    recompute(list(queryset.values_list('pk', flat=True)))


@admin.register(CatalogItem)
class CatalogItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'gtin', 'offer_count', 'lowest_price', 'winner', 'updated_at')
    search_fields = ('title', 'gtin')
    raw_id_fields = ('brand', 'sub_category', 'winner')
    readonly_fields = ('offer_count', 'lowest_price', 'winner')
    inlines = [OfferInline]
    actions = [recompute_selected]

from django.contrib import admin

from .models import CartReminder, EmailOptOut, ProductAlert


@admin.register(ProductAlert)
class ProductAlertAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'product', 'variant', 'kind', 'source', 'is_active',
                    'reference_price', 'target_price', 'sent_count', 'last_sent_at')
    list_filter = ('kind', 'source', 'is_active')
    raw_id_fields = ('user', 'product', 'variant')
    search_fields = ('user__email', 'product__title')


@admin.register(EmailOptOut)
class EmailOptOutAdmin(admin.ModelAdmin):
    list_display = ('user', 'category', 'created_at')
    list_filter = ('category',)
    raw_id_fields = ('user',)
    search_fields = ('user__email',)


@admin.register(CartReminder)
class CartReminderAdmin(admin.ModelAdmin):
    """Converted rows / sent rows per stage = reminder conversion rate."""
    list_display = ('id', 'user', 'stage', 'item_count', 'cart_value', 'emailed', 'sent_at', 'order', 'converted_at')
    list_filter = ('stage', 'emailed', ('converted_at', admin.EmptyFieldListFilter))
    raw_id_fields = ('user', 'order')
    search_fields = ('user__email',)
    date_hierarchy = 'sent_at'

from django.contrib import admin

from . models import *


admin.site.register(HomeSlider)
admin.site.register(Banners)
admin.site.register(CurrencyRate)


@admin.register(PromoCard)
class PromoCardAdmin(admin.ModelAdmin):
    list_display  = ['title', 'card_color', 'is_tall', 'position', 'is_active']
    list_editable = ['position', 'is_active', 'is_tall']
    list_filter   = ['card_color', 'is_active']
    search_fields = ['title', 'eyebrow']
    fieldsets = [
        ('Content', {'fields': ['title', 'eyebrow', 'link_url', 'link_text', 'image']}),
        ('Colors', {'fields': ['card_color', 'text_color', 'link_color', 'badge_text', 'badge_color']}),
        ('Layout', {'fields': ['is_tall', 'position', 'is_active']}),
    ]
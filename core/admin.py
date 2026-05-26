from django.contrib import admin

from . models import *


admin.site.register(HomeSlider)
admin.site.register(Banners)
admin.site.register(CurrencyRate)


@admin.register(SearchHistory)
class SearchHistoryAdmin(admin.ModelAdmin):
    list_display  = ['user', 'query', 'searched_at']
    list_filter   = ['searched_at']
    search_fields = ['user__email', 'query']
    readonly_fields = ['searched_at']


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


@admin.register(SocialLink)
class SocialLinkAdmin(admin.ModelAdmin):
    list_display  = ['platform', 'category', 'label', 'member_count', 'order', 'is_active', 'created_at']
    list_editable = ['order', 'is_active']
    list_filter   = ['category', 'platform', 'is_active']
    search_fields = ['label', 'description', 'url']
    ordering      = ['category', 'order']
    fieldsets = [
        ('Platform', {'fields': ['platform', 'category']}),
        ('Card Content', {'fields': ['label', 'url', 'description', 'member_count']}),
        ('Display', {'fields': ['order', 'is_active']}),
    ]
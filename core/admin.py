from django.contrib import admin

from . models import *


class ScheduledAdmin(admin.ModelAdmin):
    """Hero slides and side banners: what they say, and when they show (core/scheduling.py)."""
    list_display = ['title', 'when_it_shows', 'order', 'is_active']
    list_editable = ['order', 'is_active']
    list_filter = ['is_active', 'occasion', 'holiday']
    search_fields = ['title']
    autocomplete_fields = ['occasion', 'holiday']
    schedule_fieldset = ('When it shows', {
        'fields': [('starts_at', 'ends_at'), 'occasion', 'holiday', 'keep_in_season'],
        'description': (
            'Leave all of these blank for an <b>everyday</b> item: everyday items are what shoppers see '
            'between seasons, so keep at least one active. Set dates, an occasion or a holiday for a '
            '<b>seasonal</b> item: while a seasonal one is showing, everyday items step aside unless '
            '"Keep during seasons" is ticked.'),
    })

    @admin.display(description='When it shows')
    def when_it_shows(self, obj):
        from django.utils import timezone
        now = timezone.now()
        if not obj.is_seasonal:
            return 'Everyday' + (' (also in seasons)' if obj.keep_in_season else '')
        if obj.ends_at and obj.ends_at <= now:
            return 'Ended'
        if obj.starts_at and obj.starts_at > now:
            return f"Starts {timezone.localtime(obj.starts_at):%d %b %Y %H:%M}"
        if obj.occasion_id:
            return f'With occasion: {obj.occasion}'
        if obj.holiday_id:
            return f'Every year with: {obj.holiday}'
        return 'Showing now' + (f" until {timezone.localtime(obj.ends_at):%d %b %Y}" if obj.ends_at else '')

    def changelist_view(self, request, extra_context=None):
        from django.contrib import messages
        everyday = self.model.objects.filter(is_active=True, starts_at__isnull=True, ends_at__isnull=True,
                                             occasion__isnull=True, holiday__isnull=True)
        if not everyday.exists():
            messages.warning(request, (
                f'No active everyday {self.model._meta.verbose_name_plural.lower()}: between seasons the '
                'homepage falls back to a plain built-in one. Add one with no dates, occasion or holiday.'))
        return super().changelist_view(request, extra_context)


@admin.register(HomeSlider)
class HomeSliderAdmin(ScheduledAdmin):
    fieldsets = [
        ('Content', {'fields': ['title', 'subtitle', 'description', 'deal_type', 'price_prefix', 'price',
                                'image_desktop', 'image_mobile', 'link_url', 'cta_label']}),
        ('Look', {'fields': ['text_theme', 'content_align', 'cta_position']}),
        ScheduledAdmin.schedule_fieldset,
        ('Display', {'fields': ['order', 'is_active']}),
    ]


@admin.register(Banners)
class BannersAdmin(ScheduledAdmin):
    fieldsets = [
        ('Content', {'fields': ['title', 'subtitle', 'image', 'deal_type', 'link', 'cta_label']}),
        ('Look', {'fields': ['text_theme', 'content_align']}),
        ScheduledAdmin.schedule_fieldset,
        ('Display', {'fields': ['order', 'is_active']}),
    ]
admin.site.register(CurrencyRate)


@admin.register(SearchHistory)
class SearchHistoryAdmin(admin.ModelAdmin):
    list_display  = ['user', 'query', 'searched_at']
    list_filter   = ['searched_at']
    search_fields = ['user__email', 'query']
    readonly_fields = ['searched_at']
    raw_id_fields = ['user']
    show_full_result_count = False


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
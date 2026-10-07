from django import forms
from django.contrib.admin.helpers import ActionForm
from django.utils.safestring import mark_safe
from django.contrib import admin

# Register your models here.
from . models import *
from django.contrib import admin
from product.models import Product


# Register your models here.

@admin.register(ProductViewLog)
class ProductViewLogAdmin(admin.ModelAdmin):
    list_display  = ['product', 'visitor_key', 'is_bot', 'is_returning', 'device_type', 'date', 'viewed_at']
    list_filter   = ['is_bot', 'is_returning', 'device_type', 'date']
    search_fields = ['product__title', 'visitor_key']
    readonly_fields = ['viewed_at']


@admin.register(ProductDailyStats)
class ProductDailyStatsAdmin(admin.ModelAdmin):
    list_display  = ['product', 'date', 'total_views', 'unique_views', 'returning_views', 'bot_views']
    list_filter   = ['date']
    search_fields = ['product__title']


@admin.register(RecentlyViewedProduct)
class RecentlyViewedProductAdmin(admin.ModelAdmin):
    list_display  = ['user', 'product', 'viewed_at']
    list_filter   = ['viewed_at']
    search_fields = ['user__email', 'product__title']
    readonly_fields = ['viewed_at']

class ProductVariantsAdmin(admin.TabularInline):
    model = Variants
    show_change_link = True

class VariantImageAdmin(admin.TabularInline):
    model = VariantImage
    list_display = ['image']

class ProductImagesAdmin(admin.TabularInline):
    model = ProductImages
    readonly_fields = ('id',)
    
class ProductDeliveryOptionAdmin(admin.TabularInline):
    model = ProductDeliveryOption
    list_display = ['delivery_option']

    
class ProductAdmin(admin.ModelAdmin):
    prepopulated_fields = {'slug': ('title',)}
    list_editable = ['status']
    list_filter = ['status', 'vendor', 'sub_category']
    inlines = [ProductImagesAdmin, ProductVariantsAdmin, ProductDeliveryOptionAdmin]
    list_display = ['title', 'product_image', "price",'sub_category', 'vendor', 'status']
    readonly_fields = ['search_vector']

    # actions = ['index_selected_products', 'setup_periodic_indexing']

    # def index_selected_products(self, request, queryset):
    #     task = index_products_task.delay(timezone.now().isoformat())
    #     self.message_user(request, f"Started indexing products with task ID: {task.id}")

    # def setup_periodic_indexing(self, request, queryset):
    #     # Create or get a crontab schedule (every 6 hours)
    #     schedule, _ = CrontabSchedule.objects.get_or_create(
    #         minute='0',
    #         hour='*/6',
    #         day_of_week='*',
    #         day_of_month='*',
    #         month_of_year='*',
    #     )

    #     # Create or update the periodic task
    #     PeriodicTask.objects.update_or_create(
    #         name='Index Products Every 6 Hours',
    #         defaults={
    #             'crontab': schedule,
    #             'task': 'product.tasks.index_products_task',
    #             'enabled': True,
    #             'args': json.dumps([timezone.now().isoformat()]),
    #         }
    #     )
    #     self.message_user(request, "Periodic indexing task set up successfully")
    # setup_periodic_indexing.short_description = "Set up periodic product indexing"

class ProductVariantImageAdmin(admin.ModelAdmin):
    list_display = ['image']

class Main_CategoryAdmin(admin.ModelAdmin):
    list_display = ['title',]
    prepopulated_fields = {'slug': ('title',)}

class CategoryAdmin(admin.ModelAdmin):
    list_display = ['title', 'category_image',]
    prepopulated_fields = {'slug': ('title',)}
    
class Sub_CategoryAdmin(admin.ModelAdmin):
    list_display = ['title', 'subcategory_image','product_count']
    prepopulated_fields = {'slug': ('title',)}

class BrandAdmin(admin.ModelAdmin):
    prepopulated_fields = {'slug': ('title',)}
    list_display = ['title', 'image', 'brand_count']

class WishlistAdmin(admin.ModelAdmin):
    list_display = ['user', 'product', "saved_at"]

class ReviewMediaInline(admin.TabularInline):
    """Photos/videos on a review. Tick 'is hidden' to take one down."""
    model = ReviewMedia
    fk_name = 'review'
    extra = 0
    fields = ('preview', 'kind', 'is_hidden', 'size_bytes', 'created_at')
    readonly_fields = ('preview', 'kind', 'size_bytes', 'created_at')
    can_delete = True

    @admin.display(description='Preview')
    def preview(self, obj):
        if obj.kind == ReviewMedia.KIND_IMAGE and obj.thumbnail:
            return mark_safe(f'<a href="{obj.file.url}" target="_blank"><img src="{obj.thumbnail.url}" height="80"></a>')
        return mark_safe(f'<a href="{obj.file.url}" target="_blank">Open video</a>') if obj.file else '-'


class ReviewModerationEventInline(admin.TabularInline):
    """Read-only moderation history."""
    model = ReviewModerationEvent
    extra = 0
    can_delete = False
    fields = ('created_at', 'from_status', 'to_status', 'actor', 'reason', 'flags')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class ModerationActionForm(ActionForm):
    reason = forms.CharField(required=False, max_length=500, label='Reason',
                             help_text='Internal note; required to reject or hide. Never shown to the customer.')


class ProductReviewAdmin(admin.ModelAdmin):
    """
    Review moderation. Status only changes through the actions (approve /
    reject / hide), which go through product/review_services.moderate so the
    audit log, public statistics and notifications stay consistent.
    """
    list_display = ['date', 'product', 'user', 'rating', 'short_review', 'moderation_status',
                    'flag_list', 'is_verified_purchase', 'has_media', 'helpful_count']
    list_filter = ['moderation_status', 'rating', 'is_verified_purchase', 'has_media', ('date', admin.DateFieldListFilter)]
    search_fields = ['review', 'title', 'product__title', 'user__email', 'user__first_name', 'user__last_name']
    date_hierarchy = 'date'
    list_select_related = ['product', 'user']
    action_form = ModerationActionForm
    actions = ['approve_reviews', 'reject_reviews', 'hide_reviews']
    fields = ['product', 'user', 'order_item', 'is_verified_purchase', 'purchased_variant', 'rating', 'title', 'review',
              'moderation_status', 'moderation_flags', 'moderation_reason', 'moderated_by', 'moderated_at',
              'helpful_count', 'has_media', 'seller_reply', 'date', 'updated']
    readonly_fields = fields  # edits happen through the actions only
    inlines = [ReviewMediaInline, ReviewModerationEventInline]

    def has_add_permission(self, request):
        return False  # reviews come from verified customers, not staff

    @admin.display(description='Review')
    def short_review(self, obj):
        return (obj.review or '')[:80]

    @admin.display(description='Flags')
    def flag_list(self, obj):
        return ', '.join(obj.moderation_flags or []) or '-'

    def _run(self, request, queryset, action):
        from .review_services import ReviewError, can_moderate, moderate
        if not can_moderate(request.user):
            self.message_user(request, 'You do not have permission to moderate reviews.', level='error')
            return
        reason = request.POST.get('reason', '')
        done, skipped = 0, []
        for review in queryset:
            try:
                moderate(review, action, request.user, reason)
                done += 1
            except ReviewError as exc:
                skipped.append(f"#{review.pk}: {exc}")
        self.message_user(request, f"{done} review(s) {action}d.")
        if skipped:
            self.message_user(request, 'Skipped ' + '; '.join(skipped[:10]), level='warning')

    @admin.action(description='Approve: publish selected reviews')
    def approve_reviews(self, request, queryset):
        self._run(request, queryset, 'approve')

    @admin.action(description='Reject selected pending reviews (reason required)')
    def reject_reviews(self, request, queryset):
        self._run(request, queryset, 'reject')

    @admin.action(description='Hide selected reviews (reason required)')
    def hide_reviews(self, request, queryset):
        self._run(request, queryset, 'hide')


class ColorAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'color_tag']
    list_per_page = 10

class SizeAdmin(admin.ModelAdmin):
    list_display = ['name', 'code']

class VariantsAdmin(admin.ModelAdmin):
    inlines = [VariantImageAdmin, ProductDeliveryOptionAdmin]
    list_display = ['title', 'product_image', 'size','color', 'price', 'quantity']

class VariantImageAdmin(admin.ModelAdmin):
    list_display = ['image']


admin.site.register(Product, ProductAdmin)
admin.site.register(Main_Category, Main_CategoryAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(Sub_Category, Sub_CategoryAdmin)
admin.site.register(ProductReview, ProductReviewAdmin)
admin.site.register(Wishlist, WishlistAdmin)
admin.site.register(Color, ColorAdmin)
admin.site.register(Size, SizeAdmin)
admin.site.register(DeliveryOption)
admin.site.register(ProductDeliveryOption)
admin.site.register(Brand)
admin.site.register(Type)
admin.site.register(Variants, VariantsAdmin)
admin.site.register(VariantImage, VariantImageAdmin)
admin.site.register(Coupon)
admin.site.register(ClippedCoupon)


@admin.register(FlashSale)
class FlashSaleAdmin(admin.ModelAdmin):
    list_display  = ['__str__', 'label', 'sale_price', 'discount_percentage', 'start_time', 'end_time', 'is_active', 'sold_count']
    list_filter   = ['label', 'is_active', 'created_by']
    list_editable = ['is_active']
    search_fields = ['product__title']
    date_hierarchy = 'start_time'
    readonly_fields = ['sold_count', 'discount_percentage', 'is_live', 'stock_remaining', 'seconds_remaining']

    def discount_percentage(self, obj):
        return f"{obj.discount_percentage}%"
    discount_percentage.short_description = "Discount"


class OccasionSectionInline(admin.TabularInline):
    model = OccasionSection
    extra = 1
    autocomplete_fields = ['collection']
    fields = ['title', 'collection', 'position']


@admin.register(Occasion)
class OccasionAdmin(admin.ModelAdmin):
    list_display  = ['title', 'icon', 'is_active', 'start_date', 'end_date', 'position']
    list_editable = ['is_active', 'position']
    list_filter   = ['is_active']
    search_fields = ['title', 'slug']
    prepopulated_fields = {'slug': ('title',)}
    inlines = [OccasionSectionInline]
    fieldsets = (
        (None,         {'fields': ('title', 'slug', 'subtitle', 'icon', 'accent_color')}),
        ('Scheduling', {'fields': ('is_active', 'start_date', 'end_date', 'position')}),
    )


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display  = ['title', 'slug', 'filter_type', 'is_active', 'created_at']
    list_filter   = ['filter_type', 'is_active']
    list_editable = ['is_active']
    search_fields = ['title', 'slug']
    prepopulated_fields = {'slug': ('title',)}
    filter_horizontal = ['products']
    fieldsets = (
        (None, {'fields': ('title', 'slug', 'subtitle', 'description', 'is_active')}),
        ('Appearance', {'fields': ('banner_image', 'accent_color', 'icon')}),
        ('Product Source', {'fields': ('filter_type', 'sub_category', 'products')}),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Review reports from sellers: staff decide whether a review is hidden
# ─────────────────────────────────────────────────────────────────────────────
from product.models import ReviewReport  # noqa: E402


@admin.register(ReviewReport)
class ReviewReportAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'vendor', 'reason', 'status', 'review_rating', 'review_excerpt')
    list_filter = ('status', 'reason')
    search_fields = ('vendor__name', 'review__review')
    readonly_fields = ('review', 'vendor', 'reported_by', 'reason', 'details', 'created_at', 'resolved_at')
    actions = ['uphold', 'dismiss']

    @admin.display(description='Rating')
    def review_rating(self, obj):
        return obj.review.rating

    @admin.display(description='Review')
    def review_excerpt(self, obj):
        return (obj.review.review or '')[:80]

    @admin.action(description="Uphold: hide the review")
    def uphold(self, request, queryset):
        from django.utils import timezone
        from .review_services import ReviewError, moderate
        reports = list(queryset.filter(status='open').select_related('review'))
        for report in reports:
            try:
                moderate(report.review, 'hide', request.user,
                         f"Seller report upheld: {report.get_reason_display()}")
            except ReviewError:
                pass  # already hidden/rejected
        count = ReviewReport.objects.filter(pk__in=[r.pk for r in reports]).update(
            status='upheld', resolved_at=timezone.now())
        self.message_user(request, f"{count} report(s) upheld; reviews hidden.")

    @admin.action(description="Dismiss: keep the review published")
    def dismiss(self, request, queryset):
        from django.utils import timezone
        count = queryset.filter(status='open').update(status='dismissed', resolved_at=timezone.now())
        self.message_user(request, f"{count} report(s) dismissed.")

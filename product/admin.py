from django.contrib import admin

# Register your models here.
from . models import *
from django.contrib import admin
from product.models import Product


# Register your models here.

class ProductViewAdmin(admin.ModelAdmin):
    list_display = ['product', 'device_id', 'created_at']
    list_filter = ['created_at', 'product']
    search_fields = ['product__title', 'device_id']
    # actions = ['setup_periodic_cleanup']

    # def setup_periodic_cleanup(self, request, queryset):
    #     # Create or get a crontab schedule (every 24 hours at midnight UTC)
    #     schedule, _ = CrontabSchedule.objects.get_or_create(
    #         minute='0',
    #         hour='0',
    #         day_of_week='*',
    #         day_of_month='*',
    #         month_of_year='*',
    #     )

    #     # Create or update the periodic task
    #     PeriodicTask.objects.update_or_create(
    #         name='Clear Product Views Every 24 Hours',
    #         defaults={
    #             'crontab': schedule,
    #             'task': 'product.tasks.clear_product_views',
    #             'enabled': True,
    #             'args': json.dumps([]),  # No args needed for clear_product_views
    #         }
    #     )
    #     self.message_user(request, "Periodic ProductView cleanup task set up successfully")
    # setup_periodic_cleanup.short_description = "Set up periodic ProductView cleanup"

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

class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ['user', 'product', 'date', 'review', 'rating','rate_percentage']


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


admin.site.register(ProductView, ProductViewAdmin)
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
admin.site.register(FrequentlyBoughtTogether)


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

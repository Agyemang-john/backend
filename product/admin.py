from django.contrib import admin

# Register your models here.
from . models import *


# Register your models here.

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
    inlines = [ProductImagesAdmin, ProductVariantsAdmin, ProductDeliveryOptionAdmin]
    list_display = ['title', 'product_image', "price",'sub_category', 'vendor', 'status']

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

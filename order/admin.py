from django.contrib import admin

from order.models import *

# Register your models here.
class CartAdmin(admin.ModelAdmin):
    list_display = ['user','updated_at','total_price','total_items']
    list_filter = ['user']

# Register your models here.
class CartItemAdmin(admin.ModelAdmin):
    list_display = ['cart','product','variant','quantity','price','amount', 'date']
    list_filter = ['cart']

class OrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'user', 'payment_method', 'status', 'date_created', 'total']
    list_filter = ['user', 'order_number', 'payment_method', 'status']  # Added 'status' to filter options
    search_fields = ['order_number', 'user__email']  # Allows searching by order number and user's email
    ordering = ['-date_created']  # Orders by date created in descending order

class OrderProductAdmin(admin.ModelAdmin):
    list_display = ['order', 'product', 'variant', 'quantity', 'price', 'amount', 'status', 'date_created']
    list_filter = ['order', 'status']  # Added 'status' to filter options
    search_fields = ['order__order_number', 'product__title']  # Allows searching by order number and product title
    ordering = ['-date_created']  # Orders by date created in descending order



admin.site.register(Cart,CartAdmin)
admin.site.register(CartItem,CartItemAdmin)
admin.site.register(Order,OrderAdmin)
admin.site.register(OrderProduct,OrderProductAdmin)
admin.site.register(DeliveryRate)
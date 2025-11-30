from django.contrib import admin
from . models import *

# Register your models here.


class PaymentAdmin(admin.ModelAdmin):
    list_editable = ['verified']
    list_display = ['id','user', 'amount', 'ref', 'email', 'verified', 'date_created']

admin.site.register(Payment, PaymentAdmin)
admin.site.register(UserWallet)
admin.site.register(Subscription)
admin.site.register(Plan)
admin.site.register(Feature)


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ('vendor', 'amount', 'status', 'transaction_id', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('vendor__name', 'transaction_id', 'error_message')
    readonly_fields = ('created_at', 'updated_at')


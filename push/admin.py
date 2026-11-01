from django.contrib import admin

from .models import PushSubscription


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'app', 'kind', 'created_at', 'last_success_at', 'failures')
    list_filter = ('app', 'kind')
    raw_id_fields = ('user',)
    search_fields = ('user__email',)
    exclude = ('p256dh', 'auth')

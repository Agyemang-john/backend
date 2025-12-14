from django.contrib import admin
from userauths.models import MailMessage, User, ContactUs, Profile, SubscribedUsers
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken


from django.utils.translation import gettext_lazy as _

class UserAdmin(BaseUserAdmin):
    # Fields to be displayed in the admin list view
    list_display = ('email', 'first_name', 'is_suspended', 'role', 'is_active', 'is_staff', 'is_superuser', 'date_joined', 'last_login')
    list_filter = ('is_active', 'is_staff', 'is_superuser', 'role', 'is_suspended', 'date_joined')
    
    search_fields = ('email', 'first_name', 'last_name', 'phone')
    ordering = ('-date_joined',)
    list_per_page = 20

    # Read-only fields
    readonly_fields = ('date_joined', 'last_login')

    # Fieldsets for editing
    fieldsets = (
        (_('Personal Information'), {'fields': ('first_name', 'last_name', 'email', 'phone', 'role', 'auth_provider')}),
        (_('Permissions'), {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        (_('Important Dates'), {'fields': ('last_login', 'date_joined')}),
    )
    
    add_fieldsets = (
        (_('Personal Information'), {
            'classes': ('wide',),
            'fields': ('email', 'first_name', 'last_name', 'phone', 'role', 'password1', 'password2', 'is_active', 'is_staff', 'is_superuser'),
        }),
    )
    
    filter_horizontal = ('groups', 'user_permissions')


class ContactUsAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'email', 'subject']


class SubscribedUsersAdmin(admin.ModelAdmin):
    list_display = ('email', 'created_date')


# admin.site.register(User, UserAdmin, CustomUserAdmin)
admin.site.register(ContactUs, ContactUsAdmin)
admin.site.register(Profile)
admin.site.register(SubscribedUsers, SubscribedUsersAdmin)
admin.site.register(MailMessage)
admin.site.register(User, UserAdmin)


admin.site.site_header = "Negromart Inc."
admin.site.site_title = "Admin Dashboard"
admin.site.index_title = "Welcome to the Admin Dashboard"

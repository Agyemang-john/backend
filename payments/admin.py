from django.contrib import admin
from . models import *

# Register your models here.


class PaymentAdmin(admin.ModelAdmin):
    list_editable = ['verified']
    list_display = ['id','user', 'amount', 'ref', 'email', 'verified', 'date_created']

admin.site.register(Payment, PaymentAdmin)


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ('vendor', 'amount', 'status', 'transaction_id', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('vendor__name', 'transaction_id', 'error_message')
    readonly_fields = ('created_at', 'updated_at')


# subscriptions/admin.py

from django.contrib import admin
from django.utils.html import format_html
from django.db.models import Count
from .models import (
    SubscriptionPlan,
    VendorSubscription,
    SubscriptionUsage,
    PaystackCustomer,
    PaystackAuthorization,
    PaymentTransaction,
)


# ─────────────────────────────────────────────────────────────────────────────
# SubscriptionPlan
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "tier",
        "billing_cycle",
        "price_display",
        "max_products",
        "commission_display",
        "payout_delay_days",
        "active_subscribers",
        "is_active",
        "is_recommended",
    ]
    list_filter  = ["tier", "billing_cycle", "is_active", "is_recommended"]
    search_fields = ["name", "description"]
    ordering = ["price"]
    list_editable = ["is_active", "is_recommended"]

    fieldsets = (
        ("Plan identity", {
            "fields": ("name", "tier", "billing_cycle", "price", "description"),
        }),
        ("Product & listing limits", {
            "fields": ("max_products", "max_images_per_product", "max_categories"),
        }),
        ("Feature flags", {
            "fields": (
                "can_feature_products",
                "can_use_analytics",
                "can_offer_discounts",
                "can_access_bulk_upload",
                "can_use_storefront_customization",
                "priority_support",
                "is_featured_vendor",
            ),
        }),
        ("Commission & financials", {
            "fields": ("commission_rate", "payout_delay_days"),
        }),
        ("Visibility", {
            "fields": ("is_active", "is_recommended"),
        }),
    )

    readonly_fields = ["created_at", "updated_at"]

    def price_display(self, obj):
        return f"GHS {obj.price:,.2f}"
    price_display.short_description = "Price"
    price_display.admin_order_field = "price"

    def commission_display(self, obj):
        return f"{obj.commission_rate}%"
    commission_display.short_description = "Commission"

    def active_subscribers(self, obj):
        count = obj.vendor_subscriptions.filter(status="active").count()
        return count
    active_subscribers.short_description = "Active subs"

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _subscriber_count=Count("vendor_subscriptions")
        )


# ─────────────────────────────────────────────────────────────────────────────
# VendorSubscription
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(VendorSubscription)
class VendorSubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        "vendor",
        "plan",
        "status_badge",
        "start_date",
        "end_date",
        "days_remaining_display",
        "auto_renew",
        "payment_reference",
    ]
    list_filter  = ["status", "plan__tier", "auto_renew", "plan__billing_cycle"]
    search_fields = ["vendor__name", "vendor__email", "payment_reference"]
    ordering = ["-created_at"]
    readonly_fields = [
        "created_at", "updated_at",
        "cancelled_at", "days_remaining_display",
    ]
    date_hierarchy = "start_date"

    fieldsets = (
        ("Subscription", {
            "fields": ("vendor", "plan", "status"),
        }),
        ("Dates", {
            "fields": ("start_date", "end_date", "trial_end_date"),
        }),
        ("Renewal & cancellation", {
            "fields": ("auto_renew", "cancelled_at", "cancellation_reason"),
        }),
        ("Payment", {
            "fields": ("payment_reference",),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def status_badge(self, obj):
        colours = {
            "active":    "#16a34a",   # green
            "trial":     "#2563eb",   # blue
            "past_due":  "#d97706",   # amber
            "cancelled": "#6b7280",   # grey
            "expired":   "#dc2626",   # red
        }
        colour = colours.get(obj.status, "#6b7280")
        return format_html(
            '<span style="background:{};color:white;padding:2px 10px;'
            'border-radius:99px;font-size:11px;font-weight:600;">{}</span>',
            colour,
            obj.get_status_display(),
        )
    status_badge.short_description = "Status"

    def days_remaining_display(self, obj):
        days = obj.days_remaining()
        if days == 0:
            return format_html('<span style="color:#dc2626;font-weight:600;">Expired</span>')
        if days <= 3:
            return format_html('<span style="color:#d97706;font-weight:600;">{} days</span>', days)
        return f"{days} days"
    days_remaining_display.short_description = "Days remaining"


# ─────────────────────────────────────────────────────────────────────────────
# SubscriptionUsage
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SubscriptionUsage)
class SubscriptionUsageAdmin(admin.ModelAdmin):
    list_display = [
        "vendor",
        "active_products_count",
        "max_products_display",
        "usage_bar",
        "period_start",
        "updated_at",
    ]
    search_fields = ["vendor__name"]
    readonly_fields = ["updated_at", "usage_bar"]

    def max_products_display(self, obj):
        if obj.subscription and obj.subscription.plan:
            return obj.subscription.plan.max_products
        return "—"
    max_products_display.short_description = "Plan limit"

    def usage_bar(self, obj):
        if not obj.subscription or not obj.subscription.plan:
            return "—"
        max_p = obj.subscription.plan.max_products
        used  = obj.active_products_count
        pct   = min(round((used / max_p) * 100), 100) if max_p else 100
        colour = "#16a34a" if pct < 75 else "#d97706" if pct < 90 else "#dc2626"
        return format_html(
            '<div style="width:160px;background:#e5e7eb;border-radius:4px;height:8px;">'
            '<div style="width:{}%;background:{};height:8px;border-radius:4px;"></div>'
            '</div> <span style="font-size:11px;color:#6b7280">{}/{}</span>',
            pct, colour, used, max_p,
        )
    usage_bar.short_description = "Usage"


# ─────────────────────────────────────────────────────────────────────────────
# PaystackCustomer
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(PaystackCustomer)
class PaystackCustomerAdmin(admin.ModelAdmin):
    list_display  = ["vendor", "customer_code", "email", "created_at"]
    search_fields = ["vendor__name", "customer_code", "email"]
    readonly_fields = ["created_at"]


# ─────────────────────────────────────────────────────────────────────────────
# PaystackAuthorization
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(PaystackAuthorization)
class PaystackAuthorizationAdmin(admin.ModelAdmin):
    list_display  = [
        "vendor", "card_display", "bank",
        "is_default", "is_reusable", "created_at",
    ]
    list_filter   = ["card_type", "is_default", "is_reusable"]
    search_fields = ["vendor__name", "last4", "bank"]
    readonly_fields = ["authorization_code", "created_at"]
    # authorization_code is read-only — never let it be edited in the UI

    def card_display(self, obj):
        icons = {"visa": "💳", "mastercard": "💳", "verve": "💳"}
        icon = icons.get(obj.card_type.lower(), "💳")
        return f"{icon} {obj.card_type.title()} **** {obj.last4}  ({obj.exp_month}/{obj.exp_year})"
    card_display.short_description = "Card"


# ─────────────────────────────────────────────────────────────────────────────
# PaymentTransaction
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = [
        "vendor",
        "type_badge",
        "amount_display",
        "status_badge",
        "paystack_reference",
        "plan_name",
        "paid_at",
        "created_at",
    ]
    list_filter  = ["status", "transaction_type", "currency"]
    search_fields = [
        "vendor__name",
        "paystack_reference",
        "paystack_transaction_id",
        "subscription__plan__name",
    ]
    ordering = ["-created_at"]
    date_hierarchy = "created_at"
    readonly_fields = [
        "paystack_reference",
        "paystack_transaction_id",
        "paid_at",
        "created_at",
    ]

    # Transactions should never be edited directly — just viewed
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def amount_display(self, obj):
        return f"GHS {obj.amount:,.2f}"
    amount_display.short_description = "Amount"
    amount_display.admin_order_field = "amount"

    def status_badge(self, obj):
        colours = {
            "pending":  "#d97706",
            "success":  "#16a34a",
            "failed":   "#dc2626",
            "refunded": "#6b7280",
        }
        colour = colours.get(obj.status, "#6b7280")
        return format_html(
            '<span style="background:{};color:white;padding:2px 10px;'
            'border-radius:99px;font-size:11px;font-weight:600;">{}</span>',
            colour,
            obj.get_status_display(),
        )
    status_badge.short_description = "Status"

    def type_badge(self, obj):
        icons = {
            "initial": "🆕",
            "renewal": "🔄",
            "upgrade": "⬆️",
            "manual":  "✍️",
        }
        icon = icons.get(obj.transaction_type, "")
        return f"{icon} {obj.get_transaction_type_display()}"
    type_badge.short_description = "Type"

    def plan_name(self, obj):
        if obj.subscription and obj.subscription.plan:
            return obj.subscription.plan.name
        return "—"
    plan_name.short_description = "Plan"


from django.contrib import admin
from .momo_models import MomoAccount, BillingProfile


@admin.register(MomoAccount)
class MomoAccountAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "vendor",
        "provider",
        "masked_phone_display",
        "nickname",
        "is_default",
        "last_reference",
        "created_at",
    )

    list_filter = ("provider", "is_default", "created_at")
    search_fields = ("phone", "nickname", "vendor__name")
    ordering = ("-is_default", "-created_at")

    readonly_fields = ("created_at", "updated_at", "last_reference")

    list_editable = ("is_default", "nickname")

    def masked_phone_display(self, obj):
        return obj.masked_phone
    masked_phone_display.short_description = "Phone"

    # 🔥 Enforce ONLY one default per vendor
    def save_model(self, request, obj, form, change):
        if obj.is_default:
            MomoAccount.objects.filter(
                vendor=obj.vendor,
                is_default=True
            ).exclude(id=obj.id).update(is_default=False)

        super().save_model(request, obj, form, change)

@admin.register(BillingProfile)
class BillingProfileAdmin(admin.ModelAdmin):
    list_display = (
        "vendor",
        "full_name_display",
        "email",
        "phone",
        "country",
        "is_complete_display",
        "created_at",
    )

    search_fields = (
        "first_name",
        "last_name",
        "email",
        "vendor__name",
    )

    list_filter = ("country", "created_at")

    readonly_fields = ("created_at", "updated_at")

    def full_name_display(self, obj):
        return obj.full_name
    full_name_display.short_description = "Full Name"

    def is_complete_display(self, obj):
        return obj.is_complete
    is_complete_display.boolean = True
    is_complete_display.short_description = "Complete?"


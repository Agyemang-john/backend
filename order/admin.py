from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.utils import timezone
from django.db.models import Count, Prefetch

from order.models import (
    Cart, CartItem, Order, OrderProduct,
    DeliveryRate, CampusZone, Shipment, TrackingEvent,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

STATUS_COLORS = {
    # Order / OrderProduct
    'pending':          ('#F57F17', '#FFF8E1'),
    'processing':       ('#1565C0', '#E3F2FD'),
    'shipped':          ('#6A1B9A', '#F3E5F5'),
    'delivered':        ('#2E7D32', '#E8F5E9'),
    'canceled':         ('#757575', '#F5F5F5'),
    # Shipment extras
    'label_created':    ('#0277BD', '#E1F5FE'),
    'in_transit':       ('#6A1B9A', '#F3E5F5'),
    'out_for_delivery': ('#E65100', '#FFF3E0'),
    'failed':           ('#C62828', '#FFEBEE'),
    'returned':         ('#BF360C', '#FBE9E7'),
}

def colored_status(status):
    color, bg = STATUS_COLORS.get(status, ('#424242', '#F5F5F5'))
    label = status.replace('_', ' ').title()
    return format_html(
        '<span style="background:{};color:{};padding:2px 8px;border-radius:4px;'
        'font-size:11px;font-weight:700;letter-spacing:0.03em">{}</span>',
        bg, color, label,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cart
# ─────────────────────────────────────────────────────────────────────────────

class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    readonly_fields = ('product', 'variant', 'quantity', 'price', 'amount', 'created_at')
    can_delete = False


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ('user', 'updated_at', 'total_price', 'total_items')
    list_filter = ('updated_at',)
    search_fields = ('user__email',)
    inlines = (CartItemInline,)


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ('cart', 'product', 'variant', 'quantity', 'price', 'amount', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('cart__user__email', 'product__title')


# ─────────────────────────────────────────────────────────────────────────────
# TrackingEvent inline (used inside ShipmentAdmin)
# ─────────────────────────────────────────────────────────────────────────────

class TrackingEventInline(admin.TabularInline):
    model = TrackingEvent
    extra = 1
    fields = ('status', 'description', 'location', 'city', 'country', 'event_date')
    ordering = ('-event_date',)
    show_change_link = True


# ─────────────────────────────────────────────────────────────────────────────
# Shipment inline (used inside OrderAdmin)
# ─────────────────────────────────────────────────────────────────────────────

class ShipmentInline(admin.StackedInline):
    model = Shipment
    extra = 0
    fields = (
        'shipment_id', 'vendor', 'carrier', 'tracking_number', 'tracking_url',
        'status', 'estimated_delivery_date', 'shipped_at', 'delivered_at', 'is_international',
    )
    readonly_fields = ('shipment_id',)
    show_change_link = True


# ─────────────────────────────────────────────────────────────────────────────
# Shipment admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display  = (
        'shipment_id_link', 'order_link', 'vendor_name', 'carrier',
        'tracking_number_display', 'status_badge', 'progress_bar',
        'estimated_delivery_date', 'event_count', 'created_at',
    )
    list_filter   = ('status', 'carrier', 'is_international', 'created_at')
    search_fields = (
        'shipment_id', 'tracking_number', 'order__order_number',
        'vendor__name', 'vendor__email',
    )
    ordering      = ('-created_at',)
    readonly_fields = ('shipment_id', 'created_at', 'updated_at', 'progress_display')
    inlines       = (TrackingEventInline,)
    date_hierarchy = 'created_at'
    list_per_page  = 30

    fieldsets = (
        ('Shipment Identity', {
            'fields': ('shipment_id', 'order', 'vendor', 'items'),
        }),
        ('Carrier & Tracking', {
            'fields': ('carrier', 'carrier_code', 'tracking_number', 'tracking_url', 'is_international'),
        }),
        ('Status & Dates', {
            'fields': ('status', 'progress_display', 'shipped_at', 'delivered_at', 'estimated_delivery_date'),
        }),
        ('Timestamps', {
            'classes': ('collapse',),
            'fields': ('created_at', 'updated_at'),
        }),
    )

    actions = ('mark_delivered', 'mark_in_transit', 'mark_out_for_delivery', 'broadcast_update')

    # ── List display helpers ──────────────────────────────────────────────────

    def shipment_id_link(self, obj):
        url = reverse('admin:order_shipment_change', args=[obj.pk])
        return format_html('<a href="{}" style="font-family:monospace;font-weight:700">{}</a>', url, obj.shipment_id)
    shipment_id_link.short_description = 'Shipment ID'
    shipment_id_link.admin_order_field = 'shipment_id'

    def order_link(self, obj):
        url = reverse('admin:order_order_change', args=[obj.order_id])
        return format_html('<a href="{}">{}</a>', url, obj.order.order_number)
    order_link.short_description = 'Order'

    def vendor_name(self, obj):
        return obj.vendor.name
    vendor_name.short_description = 'Vendor'
    vendor_name.admin_order_field = 'vendor__name'

    def tracking_number_display(self, obj):
        if not obj.tracking_number:
            return format_html('<span style="color:#9E9E9E">—</span>')
        if obj.tracking_url:
            return format_html('<a href="{}" target="_blank" style="font-family:monospace">{}</a>',
                               obj.tracking_url, obj.tracking_number)
        return format_html('<span style="font-family:monospace">{}</span>', obj.tracking_number)
    tracking_number_display.short_description = 'Tracking #'

    def status_badge(self, obj):
        return colored_status(obj.status)
    status_badge.short_description = 'Status'
    status_badge.admin_order_field = 'status'

    def progress_bar(self, obj):
        pct = obj.progress_percentage
        color = '#2E7D32' if pct == 100 else '#1565C0' if pct >= 60 else '#F57F17'
        return format_html(
            '<div style="width:100px;background:#E0E0E0;border-radius:4px;overflow:hidden">'
            '<div style="width:{pct}%;background:{color};height:8px;border-radius:4px"></div>'
            '</div>'
            '<span style="font-size:11px;color:#757575">{pct}%</span>',
            pct=pct, color=color,
        )
    progress_bar.short_description = 'Progress'

    def event_count(self, obj):
        count = obj.tracking_events.count()
        return format_html('<span style="font-weight:700">{}</span>', count) if count else '—'
    event_count.short_description = '# Events'

    def progress_display(self, obj):
        return self.progress_bar(obj)
    progress_display.short_description = 'Progress'

    # ── Actions ───────────────────────────────────────────────────────────────

    def mark_delivered(self, request, queryset):
        updated = queryset.update(status='delivered', delivered_at=timezone.now())
        self._broadcast_batch(queryset)
        self.message_user(request, f'{updated} shipment(s) marked as Delivered.')
    mark_delivered.short_description = 'Mark selected as Delivered'

    def mark_in_transit(self, request, queryset):
        updated = queryset.update(status='in_transit')
        self._broadcast_batch(queryset)
        self.message_user(request, f'{updated} shipment(s) marked as In Transit.')
    mark_in_transit.short_description = 'Mark selected as In Transit'

    def mark_out_for_delivery(self, request, queryset):
        updated = queryset.update(status='out_for_delivery')
        self._broadcast_batch(queryset)
        self.message_user(request, f'{updated} shipment(s) marked as Out for Delivery.')
    mark_out_for_delivery.short_description = 'Mark selected as Out for Delivery'

    def broadcast_update(self, request, queryset):
        count = 0
        for shipment in queryset.select_related('order'):
            self._push_tracking_update(shipment.order)
            count += 1
        self.message_user(request, f'Broadcast sent for {count} shipment(s).')
    broadcast_update.short_description = 'Broadcast live tracking update to customers'

    def _broadcast_batch(self, queryset):
        seen_orders = set()
        for shipment in queryset.select_related('order'):
            if shipment.order_id not in seen_orders:
                self._push_tracking_update(shipment.order)
                seen_orders.add(shipment.order_id)

    def _push_tracking_update(self, order):
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            from order.serializers import OrderTrackingSerializer
            channel_layer = get_channel_layer()
            if not channel_layer:
                return
            order_obj = Order.objects.prefetch_related(
                'shipments__tracking_events',
                'shipments__items__product',
                'shipments__vendor',
            ).get(id=order.id)
            data = OrderTrackingSerializer(order_obj).data
            async_to_sync(channel_layer.group_send)(
                f'order_tracking_{order.id}',
                {'type': 'tracking_update', 'data': data},
            )
        except Exception:
            pass

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        self._push_tracking_update(obj.order)

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        if formset.model == TrackingEvent:
            self._push_tracking_update(form.instance.order)


# ─────────────────────────────────────────────────────────────────────────────
# TrackingEvent admin (standalone)
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TrackingEvent)
class TrackingEventAdmin(admin.ModelAdmin):
    list_display  = (
        'shipment_link', 'order_link', 'status_badge',
        'description_short', 'location_display', 'event_date', 'created_at',
    )
    list_filter   = ('status', 'event_date', 'country')
    search_fields = (
        'shipment__shipment_id', 'shipment__order__order_number',
        'description', 'city', 'country',
    )
    ordering      = ('-event_date',)
    date_hierarchy = 'event_date'
    list_per_page  = 50
    readonly_fields = ('created_at',)

    fields = ('shipment', 'status', 'description', 'location', 'city', 'country', 'event_date', 'created_at')

    def shipment_link(self, obj):
        url = reverse('admin:order_shipment_change', args=[obj.shipment_id])
        return format_html('<a href="{}" style="font-family:monospace">{}</a>', url, obj.shipment.shipment_id)
    shipment_link.short_description = 'Shipment'

    def order_link(self, obj):
        url = reverse('admin:order_order_change', args=[obj.shipment.order_id])
        return format_html('<a href="{}">{}</a>', url, obj.shipment.order.order_number)
    order_link.short_description = 'Order'

    def status_badge(self, obj):
        return colored_status(obj.status)
    status_badge.short_description = 'Status'

    def description_short(self, obj):
        return obj.description[:60] + '…' if len(obj.description) > 60 else obj.description
    description_short.short_description = 'Description'

    def location_display(self, obj):
        parts = filter(None, [obj.location, obj.city, obj.country])
        return ', '.join(parts) or '—'
    location_display.short_description = 'Location'

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Sync shipment status
        status_map = {
            'in_transit': 'in_transit',
            'out_for_delivery': 'out_for_delivery',
            'delivered': 'delivered',
            'failed_attempt': 'failed',
            'returned_to_sender': 'returned',
        }
        if obj.status in status_map:
            Shipment.objects.filter(pk=obj.shipment_id).update(status=status_map[obj.status])
        # Broadcast
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            from order.serializers import OrderTrackingSerializer
            channel_layer = get_channel_layer()
            if channel_layer:
                order_obj = Order.objects.prefetch_related(
                    'shipments__tracking_events',
                    'shipments__items__product',
                    'shipments__vendor',
                ).get(id=obj.shipment.order_id)
                data = OrderTrackingSerializer(order_obj).data
                async_to_sync(channel_layer.group_send)(
                    f'order_tracking_{order_obj.id}',
                    {'type': 'tracking_update', 'data': data},
                )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# OrderProduct inline
# ─────────────────────────────────────────────────────────────────────────────

class OrderProductInline(admin.TabularInline):
    model = OrderProduct
    extra = 0
    fields = ('product', 'variant', 'quantity', 'price', 'amount', 'status', 'tracking_number', 'shipped_date', 'delivered_date')
    readonly_fields = ('product', 'variant', 'quantity', 'price', 'amount', 'tracking_number')
    show_change_link = True
    can_delete = False


# ─────────────────────────────────────────────────────────────────────────────
# Order admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display  = (
        'order_number_link', 'customer_email', 'status_badge',
        'shipment_summary', 'payment_method', 'total_display',
        'item_count', 'date_created',
    )
    list_filter   = ('status', 'payment_method', 'is_ordered', 'date_created')
    search_fields = ('order_number', 'user__email', 'user__first_name', 'user__last_name')
    ordering      = ('-date_created',)
    date_hierarchy = 'date_created'
    list_per_page  = 30
    readonly_fields = ('order_number', 'date_created', 'date_updated', 'tracking_overview')

    inlines = (OrderProductInline, ShipmentInline)

    fieldsets = (
        ('Order Identity', {
            'fields': ('order_number', 'user', 'vendors', 'address'),
        }),
        ('Payment', {
            'fields': ('payment_method', 'payment_id', 'total', 'status', 'is_ordered'),
        }),
        ('Tracking Overview', {
            'fields': ('tracking_overview',),
        }),
        ('Metadata', {
            'classes': ('collapse',),
            'fields': ('ip', 'adminnote', 'response_date', 'date_created', 'date_updated'),
        }),
    )

    actions = (
        'mark_processing', 'mark_shipped', 'mark_delivered', 'mark_canceled',
        'broadcast_tracking_update',
    )

    # ── List display helpers ──────────────────────────────────────────────────

    def order_number_link(self, obj):
        url = reverse('admin:order_order_change', args=[obj.pk])
        return format_html(
            '<a href="{}" style="font-family:monospace;font-weight:700">{}</a>',
            url, obj.order_number,
        )
    order_number_link.short_description = 'Order #'
    order_number_link.admin_order_field = 'order_number'

    def customer_email(self, obj):
        if not obj.user:
            return '—'
        url = reverse('admin:userauths_user_change', args=[obj.user_id])
        return format_html('<a href="{}">{}</a>', url, obj.user.email)
    customer_email.short_description = 'Customer'
    customer_email.admin_order_field = 'user__email'

    def status_badge(self, obj):
        return colored_status(obj.status)
    status_badge.short_description = 'Status'
    status_badge.admin_order_field = 'status'

    def total_display(self, obj):
        return format_html('<strong>GHS {}</strong>', f'{obj.total:,.2f}')
    total_display.short_description = 'Total'
    total_display.admin_order_field = 'total'

    def item_count(self, obj):
        return obj.order_products.count()
    item_count.short_description = '# Items'

    def shipment_summary(self, obj):
        shipments = obj.shipments.all()
        if not shipments:
            return format_html('<span style="color:#9E9E9E;font-size:11px">No shipments</span>')
        parts = []
        for sh in shipments:
            parts.append(colored_status(sh.status))
        return format_html(' '.join(str(p) for p in parts))
    shipment_summary.short_description = 'Shipments'

    def tracking_overview(self, obj):
        shipments = obj.shipments.prefetch_related('tracking_events').all()
        if not shipments.exists():
            return 'No shipments yet.'
        lines = []
        for sh in shipments:
            latest = sh.tracking_events.order_by('-event_date').first()
            pct = sh.progress_percentage
            color = '#2E7D32' if pct == 100 else '#1565C0' if pct >= 60 else '#F57F17'
            lines.append(format_html(
                '<div style="margin-bottom:12px;padding:10px;border:1px solid #E0E0E0;border-radius:6px">'
                '<strong style="font-family:monospace">{sid}</strong> · {vendor} · {status}'
                '<div style="margin-top:6px;width:200px;background:#E0E0E0;border-radius:4px;overflow:hidden">'
                '<div style="width:{pct}%;background:{color};height:8px"></div></div>'
                '<div style="font-size:11px;color:#757575;margin-top:2px">{pct}% complete</div>'
                '{latest}'
                '</div>',
                sid=sh.shipment_id,
                vendor=sh.vendor.name,
                status=colored_status(sh.status),
                pct=pct,
                color=color,
                latest=format_html(
                    '<div style="margin-top:4px;font-size:12px;color:#424242">Latest: {} — {}</div>',
                    latest.description, latest.event_date.strftime('%b %d, %Y %H:%M')
                ) if latest else format_html('<div style="margin-top:4px;font-size:12px;color:#9E9E9E">No events yet</div>'),
            ))
        return format_html(''.join(str(l) for l in lines))
    tracking_overview.short_description = 'Tracking Overview'

    # ── Actions ───────────────────────────────────────────────────────────────

    def _set_status(self, request, queryset, new_status):
        updated = queryset.update(status=new_status)
        for order in queryset:
            self._push_ws(order)
        self.message_user(request, f'{updated} order(s) set to "{new_status}".')

    def mark_processing(self, request, queryset):
        self._set_status(request, queryset, 'processing')
    mark_processing.short_description = 'Set status → Processing'

    def mark_shipped(self, request, queryset):
        self._set_status(request, queryset, 'shipped')
    mark_shipped.short_description = 'Set status → Shipped'

    def mark_delivered(self, request, queryset):
        self._set_status(request, queryset, 'delivered')
    mark_delivered.short_description = 'Set status → Delivered'

    def mark_canceled(self, request, queryset):
        self._set_status(request, queryset, 'canceled')
    mark_canceled.short_description = 'Set status → Canceled'

    def broadcast_tracking_update(self, request, queryset):
        count = 0
        for order in queryset:
            self._push_ws(order)
            count += 1
        self.message_user(request, f'Live tracking update broadcast for {count} order(s).')
    broadcast_tracking_update.short_description = 'Broadcast live tracking update to customers'

    def _push_ws(self, order):
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            from order.serializers import OrderTrackingSerializer
            channel_layer = get_channel_layer()
            if not channel_layer:
                return
            order_obj = Order.objects.prefetch_related(
                'shipments__tracking_events',
                'shipments__items__product',
                'shipments__vendor',
            ).get(id=order.id)
            data = OrderTrackingSerializer(order_obj).data
            async_to_sync(channel_layer.group_send)(
                f'order_tracking_{order.id}',
                {'type': 'tracking_update', 'data': data},
            )
        except Exception:
            pass

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change:
            self._push_ws(obj)


# ─────────────────────────────────────────────────────────────────────────────
# OrderProduct admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(OrderProduct)
class OrderProductAdmin(admin.ModelAdmin):
    list_display  = ('order_link', 'product_title', 'quantity', 'price', 'amount', 'status_badge', 'date_created')
    list_filter   = ('status', 'date_created')
    search_fields = ('order__order_number', 'product__title')
    ordering      = ('-date_created',)
    readonly_fields = ('date_created', 'date_updated', 'amount', 'tracking_number')

    def order_link(self, obj):
        url = reverse('admin:order_order_change', args=[obj.order_id])
        return format_html('<a href="{}" style="font-family:monospace">{}</a>', url, obj.order.order_number)
    order_link.short_description = 'Order'

    def product_title(self, obj):
        return obj.product.title
    product_title.short_description = 'Product'
    product_title.admin_order_field = 'product__title'

    def status_badge(self, obj):
        return colored_status(obj.status)
    status_badge.short_description = 'Status'


# ─────────────────────────────────────────────────────────────────────────────
# Misc
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(DeliveryRate)
class DeliveryRateAdmin(admin.ModelAdmin):
    list_display = ('rate_per_km', 'base_price')


@admin.register(CampusZone)
class CampusZoneAdmin(admin.ModelAdmin):
    list_display = ('name', 'center_lat', 'center_lon', 'radius_km', 'flat_fee', 'free_delivery_threshold')
    search_fields = ('name',)

"""
tools/views.py
API of the paid seller tools (/api/v1/tools/...). Each view checks the plan
(tools/access.require_tool) and the team member's capability.
"""
from decimal import Decimal

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from product.models import Product
from vendor.access import ANY_MEMBER, Capability, get_current_vendor, require_capability

from . import (broadcasts, bulk_edit, chat_tools, demand, forecast, labels, listing_doctor, pricing, profit,
               promotions, registry, seller_api, storefront, webhooks)
from .access import tool_permissions
from .models import (ApiKey, AwaySetting, Broadcast, PriceChange, PromotionRule, RepricerRule, SavedReply,
                     StoreBranding, StoreLink, Webhook, WebhookDelivery)


def _csv(text, filename):
    response = HttpResponse(text, content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ToolsOverviewView(APIView):
    """Every tool, whether the store's plan includes it, and the plan that unlocks it."""
    permission_classes = [IsAuthenticated, require_capability(ANY_MEMBER)]

    def get(self, request):
        return Response({'tools': registry.summary(get_current_vendor(request.user))})


# ── What shoppers want ───────────────────────────────────────────────────────

class DemandView(APIView):
    permission_classes = tool_permissions('demand_insights', Capability.MANAGE_CATALOG, read=ANY_MEMBER)

    def get(self, request):
        return Response(demand.for_vendor(get_current_vendor(request.user)))


# ── Promotions autopilot ─────────────────────────────────────────────────────

def _promotion_json(kind, rule):
    return {
        'kind': kind, 'label': dict(PromotionRule.KIND_CHOICES)[kind],
        'is_active': bool(rule and rule.is_active),
        'settings': rule.settings if rule else promotions.DEFAULTS[kind],
        'last_run_at': rule.last_run_at.isoformat() if rule and rule.last_run_at else None,
        'last_result': rule.last_result if rule else '',
        'stats': promotions.stats(rule) if rule else None,
    }


class PromotionsView(APIView):
    permission_classes = tool_permissions('promotions', Capability.MANAGE_CATALOG, read=ANY_MEMBER)

    def get(self, request):
        vendor = get_current_vendor(request.user)
        rules = {r.kind: r for r in PromotionRule.objects.filter(vendor=vendor)}
        return Response({'automations': [_promotion_json(k, rules.get(k)) for k, _ in PromotionRule.KIND_CHOICES],
                         'max_sale_products': promotions.MAX_SALE_PRODUCTS})


class PromotionView(APIView):
    """PUT {is_active, settings}: switch an automation on/off and change its settings."""
    permission_classes = tool_permissions('promotions', Capability.MANAGE_CATALOG)

    def put(self, request, kind):
        vendor = get_current_vendor(request.user)
        if kind not in dict(PromotionRule.KIND_CHOICES):
            return Response({'error': 'Unknown automation.'}, status=status.HTTP_404_NOT_FOUND)
        active = bool(request.data.get('is_active', True))
        try:
            cleaned = promotions.clean_settings(vendor, kind, request.data.get('settings') or {})
        except promotions.PromotionError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        rule, _ = PromotionRule.objects.update_or_create(vendor=vendor, kind=kind,
                                                         defaults={'is_active': active, 'settings': cleaned})
        if kind == PromotionRule.WEEKLY_SALE:
            promotions.cancel_upcoming(rule)          # new terms replace sales not started yet
            if active:
                promotions.run_rule(rule)
            rule.refresh_from_db()
        return Response(_promotion_json(kind, rule))


class ToolProductsView(APIView):
    """The store's live products (without versions unless ?all=1): the product picker of the tools pages."""
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG, read=ANY_MEMBER)]

    def get(self, request):
        vendor = get_current_vendor(request.user)
        q = (request.query_params.get('search') or '').strip()[:80]
        qs = Product.objects.filter(vendor=vendor, status='published')
        if request.query_params.get('all') != '1':
            qs = qs.filter(variant='None')
        if q:
            qs = qs.filter(title__icontains=q)
        return Response({'results': [{'id': p.pk, 'title': p.title, 'price': str(p.price)}
                                     for p in qs.order_by('title')[:50]]})


# ── Messages to followers ────────────────────────────────────────────────────

class BroadcastsView(APIView):
    permission_classes = tool_permissions('broadcasts', Capability.MANAGE_CATALOG, read=ANY_MEMBER)

    def get(self, request):
        vendor = get_current_vendor(request.user)
        item = next(t for t in registry.summary(vendor) if t['key'] == 'broadcasts')
        wait = broadcasts.next_allowed(vendor)
        return Response({
            'allowance': item.get('allowance', 0), 'used': item.get('used', 0),
            'audience_sizes': broadcasts.sizes(vendor),
            'next_allowed_at': wait.isoformat() if wait else None,
            'results': [{'id': b.pk, 'subject': b.subject, 'message': b.message, 'audience': b.audience,
                         'status': b.status, 'recipients': b.recipients, 'emailed': b.emailed,
                         'created_at': b.created_at.isoformat(), 'sent_at': b.sent_at.isoformat() if b.sent_at else None}
                        for b in Broadcast.objects.filter(vendor=vendor).order_by('-created_at')[:30]],
        })

    def post(self, request):
        vendor = get_current_vendor(request.user)
        d = request.data
        try:
            b = broadcasts.create(vendor, request.user, d.get('audience'), d.get('subject'), d.get('message'),
                                  d.get('product_ids') or [])
        except broadcasts.BroadcastError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'id': b.pk, 'status': b.status}, status=status.HTTP_201_CREATED)


# ── Labels & pick lists ──────────────────────────────────────────────────────

class LabelOrdersView(APIView):
    """Orders waiting to ship (paid, no shipment from this store yet), oldest first."""
    permission_classes = tool_permissions('labels', Capability.MANAGE_ORDERS)

    def get(self, request):
        from django.db.models import Count, Q, Sum
        from order.fulfilment import awaiting_dispatch_orders
        vendor = get_current_vendor(request.user)
        orders = (awaiting_dispatch_orders(vendor).select_related('address')
                  .annotate(units=Sum('order_products__quantity', filter=Q(order_products__product__vendor=vendor)),
                            lines=Count('order_products', filter=Q(order_products__product__vendor=vendor)))
                  .order_by('date_created')[:200])
        return Response({'results': [{
            'id': o.pk, 'order_number': o.order_number, 'date': o.date_created.isoformat(),
            'name': (o.address.full_name if o.address_id else '') or '', 'town': (o.address.town if o.address_id else '') or '',
            'pickup': bool(o.address_id and o.address.pickup_point_id), 'units': o.units or 0, 'lines': o.lines or 0,
        } for o in orders], 'max_orders': labels.MAX_ORDERS})


class LabelsPdfView(APIView):
    """POST {order_ids, document: labels|packing_slips|pick_list, size: 4x6|a4} → PDF."""
    permission_classes = tool_permissions('labels', Capability.MANAGE_ORDERS)

    def post(self, request):
        vendor = get_current_vendor(request.user)
        ids = [int(i) for i in (request.data.get('order_ids') or []) if str(i).isdigit()]
        document = request.data.get('document', 'labels')
        if not ids:
            return Response({'error': 'Choose at least one order.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(ids) > labels.MAX_ORDERS:
            return Response({'error': f'Up to {labels.MAX_ORDERS} orders at a time.'}, status=status.HTTP_400_BAD_REQUEST)
        orders = labels.orders_for(vendor, ids)
        if not orders:
            return Response({'error': 'None of these orders have items from your store.'}, status=status.HTTP_404_NOT_FOUND)
        if document == 'packing_slips':
            pdf = labels.packing_slips_pdf(vendor, orders)
        elif document == 'pick_list':
            pdf = labels.pick_list_pdf(vendor, orders)
        elif document == 'labels':
            pdf = labels.labels_pdf(vendor, orders, size='a4' if request.data.get('size') == 'a4' else '4x6')
        else:
            return Response({'error': 'Unknown document.'}, status=status.HTTP_400_BAD_REQUEST)
        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{document}_{timezone.localtime():%Y%m%d_%H%M}.pdf"'
        return response


# ── Chat helpers ─────────────────────────────────────────────────────────────

def _reply_json(r):
    return {'id': r.pk, 'title': r.title, 'body': r.body, 'use_count': r.use_count}


class SavedRepliesView(APIView):
    permission_classes = tool_permissions('chat_tools', Capability.MANAGE_MESSAGES)

    def get(self, request):
        vendor = get_current_vendor(request.user)
        return Response({'results': [_reply_json(r) for r in SavedReply.objects.filter(vendor=vendor)],
                         'max': chat_tools.MAX_REPLIES})

    def post(self, request):
        vendor = get_current_vendor(request.user)
        try:
            reply = chat_tools.save_reply(vendor, request.data.get('title'), request.data.get('body'))
        except chat_tools.ChatToolError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_reply_json(reply), status=status.HTTP_201_CREATED)


class SavedReplyView(APIView):
    permission_classes = tool_permissions('chat_tools', Capability.MANAGE_MESSAGES)

    def put(self, request, pk):
        vendor = get_current_vendor(request.user)
        reply = get_object_or_404(SavedReply, pk=pk, vendor=vendor)
        try:
            reply = chat_tools.save_reply(vendor, request.data.get('title'), request.data.get('body'), reply=reply)
        except chat_tools.ChatToolError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_reply_json(reply))

    def post(self, request, pk):
        """The team used it (ranks the most used first)."""
        chat_tools.count_use(get_current_vendor(request.user), pk)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def delete(self, request, pk):
        SavedReply.objects.filter(pk=pk, vendor=get_current_vendor(request.user)).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AwayView(APIView):
    permission_classes = tool_permissions('chat_tools', Capability.MANAGE_MESSAGES)

    def get(self, request):
        vendor = get_current_vendor(request.user)
        s = AwaySetting.objects.filter(vendor=vendor).first()
        return Response({'mode': s.mode if s else AwaySetting.OFF, 'message': s.message if s else '',
                         'has_hours': vendor.opening_hours.exists(),
                         'away_now': chat_tools.is_away(vendor, s) if s else False,
                         'stats': chat_tools.response_stats(vendor)})

    def put(self, request):
        vendor = get_current_vendor(request.user)
        try:
            s = chat_tools.save_away(vendor, request.data.get('mode'), request.data.get('message'))
        except chat_tools.ChatToolError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'mode': s.mode, 'message': s.message, 'away_now': chat_tools.is_away(vendor, s)})


# ── Store link & page ────────────────────────────────────────────────────────

def _store_json(vendor):
    from django.conf import settings
    link = StoreLink.objects.filter(vendor=vendor).first()
    b = StoreBranding.objects.filter(vendor=vendor).first()
    site = settings.SITE_URL.rstrip('/')
    return {
        'handle': link.handle if link else '', 'visits': link.visits if link else 0,
        'short_url': f'{site}/s/{link.handle}' if link else '', 'store_url': f'{site}/seller/{vendor.slug}',
        'store_name': vendor.name,
        'branding': {'accent_color': b.accent_color if b else '', 'tagline': b.tagline if b else '',
                     'announcement': b.announcement if b else '', 'featured_product_ids': b.featured_product_ids if b else []},
        'max_featured': storefront.MAX_FEATURED,
    }


class StoreToolView(APIView):
    """GET the link and branding; PUT {handle?} or {branding: {...}}."""
    permission_classes = tool_permissions('store_link', Capability.MANAGE_STORE, read=ANY_MEMBER)

    def get(self, request):
        return Response(_store_json(get_current_vendor(request.user)))

    def put(self, request):
        vendor = get_current_vendor(request.user)
        try:
            if 'handle' in request.data:
                storefront.set_handle(vendor, request.data.get('handle'))
            if isinstance(request.data.get('branding'), dict):
                storefront.save_branding(vendor, **{k: v for k, v in request.data['branding'].items() if k in (
                    'accent_color', 'tagline', 'announcement', 'featured_product_ids')})
        except storefront.StoreToolError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_store_json(vendor))


class StoreLinkResolveView(APIView):
    """Public: /s/<handle> on the shop site asks here where to send the shopper (and counts the visit)."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, handle):
        slug = storefront.resolve(handle, request)
        if not slug:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'slug': slug})


# ── API & stock sync: set-up in Seller Centre ────────────────────────────────

def _key_json(k):
    return {'id': k.pk, 'name': k.name, 'prefix': f'nm_{k.prefix}_…', 'can_write': k.can_write,
            'created_at': k.created_at, 'last_used_at': k.last_used_at}


def _hook_json(h, recent=()):
    return {'id': h.pk, 'url': h.url, 'events': h.events, 'is_active': h.is_active, 'secret': h.secret,
            'disabled_reason': h.disabled_reason, 'last_delivery_at': h.last_delivery_at,
            'recent': [{'id': d.pk, 'event': d.event, 'status': d.status, 'attempts': d.attempts,
                        'response_code': d.response_code, 'error': d.error, 'created_at': d.created_at}
                       for d in recent]}


class ApiSetupView(APIView):
    """GET keys and webhooks; POST {name, can_write} makes a key (the full key is in this answer only)."""
    permission_classes = tool_permissions('api', Capability.MANAGE_STORE)

    def get(self, request):
        vendor = get_current_vendor(request.user)
        hooks = list(Webhook.objects.filter(vendor=vendor))      # at most MAX_WEBHOOKS
        return Response({
            'keys': [_key_json(k) for k in ApiKey.objects.filter(vendor=vendor, revoked_at__isnull=True)],
            'webhooks': [_hook_json(h, WebhookDelivery.objects.filter(webhook=h).order_by('-created_at')[:10])
                         for h in hooks],
            'events': webhooks.EVENTS, 'max_keys': seller_api.MAX_KEYS, 'max_webhooks': webhooks.MAX_WEBHOOKS,
        })

    def post(self, request):
        vendor = get_current_vendor(request.user)
        try:
            key, raw = seller_api.create_key(vendor, request.user, request.data.get('name'),
                                             request.data.get('can_write', True))
        except seller_api.SellerApiError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({**_key_json(key), 'key': raw}, status=status.HTTP_201_CREATED)


class ApiKeyView(APIView):
    permission_classes = tool_permissions('api', Capability.MANAGE_STORE)

    def delete(self, request, pk):
        ApiKey.objects.filter(pk=pk, vendor=get_current_vendor(request.user), revoked_at__isnull=True).update(
            revoked_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)


class WebhooksView(APIView):
    permission_classes = tool_permissions('api', Capability.MANAGE_STORE)

    def post(self, request):
        try:
            hook = webhooks.create(get_current_vendor(request.user), request.data.get('url'), request.data.get('events'))
        except webhooks.WebhookError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_hook_json(hook), status=status.HTTP_201_CREATED)


class WebhookView(APIView):
    """PUT {url?, events?, is_active?, roll_secret?}; POST sends a test ping; DELETE removes it."""
    permission_classes = tool_permissions('api', Capability.MANAGE_STORE)

    def _hook(self, request, pk):
        return get_object_or_404(Webhook, pk=pk, vendor=get_current_vendor(request.user))

    def put(self, request, pk):
        hook = self._hook(request, pk)
        try:
            hook = webhooks.update(hook, url=request.data.get('url'), events=request.data.get('events'),
                                   is_active=request.data.get('is_active'),
                                   roll_secret=bool(request.data.get('roll_secret')))
        except webhooks.WebhookError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_hook_json(hook))

    def post(self, request, pk):
        ok, message = webhooks.send_test(self._hook(request, pk))
        return Response({'ok': ok, 'message': message})

    def delete(self, request, pk):
        self._hook(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── API & stock sync: what the store's own systems call (API key auth) ──────

class _SellerApi(APIView):
    authentication_classes = [seller_api.ApiKeyAuthentication]
    permission_classes = [seller_api.HasApiKey]
    throttle_classes = [seller_api.ApiKeyThrottle]

    def handle_exception(self, exc):
        if isinstance(exc, seller_api.SellerApiError):
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


class ApiProductsView(_SellerApi):
    def get(self, request):
        return Response(seller_api.products(request.auth.vendor, request.query_params))


class ApiOrdersView(_SellerApi):
    def get(self, request):
        return Response(seller_api.orders(request.auth.vendor, request.query_params))


class ApiStockView(_SellerApi):
    def post(self, request):
        return Response(seller_api.update_stock(request.auth.vendor, request.data.get('items')))


# ── Bulk edit ────────────────────────────────────────────────────────────────

class BulkEditRowsView(APIView):
    permission_classes = tool_permissions('bulk_edit', Capability.MANAGE_CATALOG)

    def get(self, request):
        q = request.query_params
        return Response(bulk_edit.page(get_current_vendor(request.user), q.get('search', '').strip()[:100],
                                       q.get('status', ''), _int(q.get('page'), 1)))

    def post(self, request):
        changes = request.data.get('changes')
        if not isinstance(changes, list) or not changes:
            return Response({'error': 'Nothing to save.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = bulk_edit.apply_changes(get_current_vendor(request.user), changes)
        except bulk_edit.BulkEditError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK if not result['errors'] else status.HTTP_207_MULTI_STATUS)


class BulkEditActionView(APIView):
    """{op, value, product_ids?: [..]} — without product_ids, applies to everything the filter matches."""
    permission_classes = tool_permissions('bulk_edit', Capability.MANAGE_CATALOG)

    def post(self, request):
        vendor = get_current_vendor(request.user)
        ids = request.data.get('product_ids')
        if ids is not None and not isinstance(ids, list):
            return Response({'error': 'product_ids must be a list.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            changes = bulk_edit.action_changes(vendor, request.data.get('op'), request.data.get('value'),
                                               product_ids=ids, search=str(request.data.get('search') or '')[:100],
                                               status=str(request.data.get('status') or ''))
            if not changes:
                return Response({'updated': 0, 'errors': []})
            result = bulk_edit.apply_changes(vendor, changes)
        except bulk_edit.BulkEditError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK if not result['errors'] else status.HTTP_207_MULTI_STATUS)


class BulkEditCsvView(APIView):
    permission_classes = tool_permissions('bulk_edit', Capability.MANAGE_CATALOG)

    def get(self, request):
        q = request.query_params
        text = bulk_edit.export_csv(get_current_vendor(request.user), q.get('search', '').strip()[:100], q.get('status', ''))
        return _csv(text, f'negromart_products_{timezone.localdate():%Y%m%d}.csv')

    def post(self, request):
        upload = request.FILES.get('file')
        if not upload:
            return Response({'error': 'Choose the edited CSV file.'}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > 5 * 1024 * 1024:
            return Response({'error': 'The file is larger than 5 MB.'}, status=status.HTTP_400_BAD_REQUEST)
        raw = upload.read()
        for encoding in ('utf-8-sig', 'cp1252'):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                text = None
        if text is None:
            return Response({'error': 'Save the file as CSV UTF-8 and try again.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            changes = bulk_edit.changes_from_csv(text)
            if not changes:
                return Response({'error': 'No changes found in the file.'}, status=status.HTTP_400_BAD_REQUEST)
            result = bulk_edit.apply_changes(get_current_vendor(request.user), changes)
        except bulk_edit.BulkEditError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK if not result['errors'] else status.HTTP_207_MULTI_STATUS)


# ── Stock forecast ───────────────────────────────────────────────────────────

class StockForecastView(APIView):
    permission_classes = tool_permissions('stock_forecast', Capability.MANAGE_CATALOG, read=ANY_MEMBER)

    def get(self, request):
        q = request.query_params
        data = forecast.forecast(get_current_vendor(request.user), _int(q.get('lead_days'), 7), _int(q.get('cover_days'), 30))
        if q.get('export') == 'csv':
            import csv
            import io
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(['title', 'version', 'seller_sku', 'stock', 'sold_7d', 'sold_28d', 'per_day', 'days_left',
                             'status', 'reorder_qty'])
            for r in data['results']:
                if r['reorder_qty'] > 0 or q.get('all') == '1':
                    writer.writerow([r['title'], r['version'], r['seller_sku'], r['stock'], r['sold_7d'], r['sold_28d'],
                                     r['per_day'], '' if r['days_left'] is None else r['days_left'], r['status'],
                                     r['reorder_qty']])
            return _csv('﻿' + out.getvalue(), f'reorder_list_{timezone.localdate():%Y%m%d}.csv')
        return Response(data)


# ── Listing doctor ───────────────────────────────────────────────────────────

class ListingScoresView(APIView):
    permission_classes = tool_permissions('listing_doctor', Capability.MANAGE_CATALOG, read=ANY_MEMBER)

    def get(self, request):
        q = request.query_params
        vendor = get_current_vendor(request.user)
        data = listing_doctor.page(vendor, _int(q.get('page'), 1), q.get('order', 'worst'))
        item = next(t for t in registry.summary(vendor) if t['key'] == 'listing_doctor')
        data.update({'allowance': item.get('allowance', 0), 'used': item.get('used', 0)})
        return Response(data)


class ListingSuggestView(APIView):
    permission_classes = tool_permissions('listing_doctor', Capability.MANAGE_CATALOG)

    def post(self, request, pk):
        vendor = get_current_vendor(request.user)
        product = get_object_or_404(Product.objects.select_related('sub_category', 'brand'), pk=pk, vendor=vendor)
        try:
            return Response(listing_doctor.suggest(vendor, product, str(request.data.get('notes') or '')))
        except listing_doctor.AssistantUnavailable as exc:
            return Response({'error': str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS
                            if 'used all' in str(exc) else status.HTTP_503_SERVICE_UNAVAILABLE)


class ListingApplyView(APIView):
    permission_classes = tool_permissions('listing_doctor', Capability.MANAGE_CATALOG)

    def post(self, request, pk):
        vendor = get_current_vendor(request.user)
        product = get_object_or_404(Product, pk=pk, vendor=vendor)
        d = request.data
        try:
            listing_doctor.apply_suggestion(vendor, product, d.get('title'), d.get('description'), d.get('features'))
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'ok': True, 'title': product.title})


# ── Profit report ────────────────────────────────────────────────────────────

class ProfitReportView(APIView):
    permission_classes = tool_permissions('profit_report', Capability.MANAGE_FINANCE, read=Capability.VIEW_FINANCE)

    def get(self, request):
        q = request.query_params
        vendor = get_current_vendor(request.user)
        start, end = profit.parse_date(q.get('start')), profit.parse_date(q.get('end'))
        if q.get('export') == 'csv':
            return _csv(profit.report_csv(vendor, start, end), f'profit_{timezone.localdate():%Y%m%d}.csv')
        return Response(profit.report(vendor, start, end))


# ── Pricing ──────────────────────────────────────────────────────────────────

class PriceInsightsView(APIView):
    permission_classes = tool_permissions('price_insights', Capability.MANAGE_CATALOG, read=ANY_MEMBER)

    def get(self, request):
        q = request.query_params
        vendor = get_current_vendor(request.user)
        from payments.entitlements import plan_for_vendor
        data = pricing.insights(vendor, _int(q.get('page'), 1), q.get('only', 'all'))
        data['repricer_included'] = registry.included(plan_for_vendor(vendor), 'auto_repricer')
        return Response(data)


class RuleSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    strategy = serializers.ChoiceField(choices=RepricerRule.STRATEGY_CHOICES, default=RepricerRule.BEAT)
    step = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('0.01'), default=Decimal('1'))
    min_price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    max_price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    is_active = serializers.BooleanField(default=True)

    def validate(self, attrs):
        if attrs['min_price'] > attrs['max_price']:
            raise serializers.ValidationError({'max_price': 'The maximum must be at least the minimum.'})
        return attrs


class RepricerRulesView(APIView):
    permission_classes = tool_permissions('auto_repricer', Capability.MANAGE_CATALOG, read=ANY_MEMBER)

    def get(self, request):
        vendor = get_current_vendor(request.user)
        rules = RepricerRule.objects.filter(vendor=vendor).select_related('product').order_by('-updated_at')
        return Response({'results': [{**pricing.rule_json(r), 'product_id': r.product_id, 'title': r.product.title,
                                      'price': str(r.product.price)} for r in rules]})

    def post(self, request):
        vendor = get_current_vendor(request.user)
        ser = RuleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        product = get_object_or_404(Product, pk=d['product_id'], vendor=vendor)
        if not product.catalog_item_id:
            return Response({'error': 'The repricer works on items other stores also sell. This one has no '
                                      'competing offers.'}, status=status.HTTP_400_BAD_REQUEST)
        if product.variant != 'None':
            return Response({'error': 'Products with versions keep a price per version; set those in bulk edit.'},
                            status=status.HTTP_400_BAD_REQUEST)
        rule, _ = RepricerRule.objects.update_or_create(
            product=product, defaults={'vendor': vendor, 'strategy': d['strategy'], 'step': d['step'],
                                       'min_price': d['min_price'], 'max_price': d['max_price'],
                                       'is_active': d['is_active'], 'last_run_at': None})
        return Response(pricing.rule_json(rule), status=status.HTTP_201_CREATED)


class RepricerRuleView(APIView):
    permission_classes = tool_permissions('auto_repricer', Capability.MANAGE_CATALOG)

    def delete(self, request, product_id):
        RepricerRule.objects.filter(vendor=get_current_vendor(request.user), product_id=product_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PriceChangesView(APIView):
    """The last price changes made by bulk edit and the repricer."""
    permission_classes = tool_permissions('price_insights', Capability.MANAGE_CATALOG, read=ANY_MEMBER)

    def get(self, request):
        vendor = get_current_vendor(request.user)
        rows = PriceChange.objects.filter(vendor=vendor).select_related('product').order_by('-created_at')[:100]
        return Response({'results': [{
            'product_id': c.product_id, 'title': c.product.title, 'variant_id': c.variant_id,
            'old_price': str(c.old_price), 'new_price': str(c.new_price), 'source': c.source, 'note': c.note,
            'at': c.created_at.isoformat()} for c in rows]})

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from product.models import Product, Sub_Category
from vendor.access import Capability, get_current_vendor, require_capability

from . import services
from .models import AttributeDefinition

MAX_COMPARE = 4


class SubCategoryAttributesView(APIView):
    """The attributes that apply to a sub-category (for the seller form and filters)."""
    permission_classes = [AllowAny]

    def get(self, request, pk):
        sub = get_object_or_404(Sub_Category.objects.only('pk', 'category_id'), pk=pk)
        return Response({'results': [services.definition_json(d) for d in services.definitions_for(sub)]})


class SellerProductAttributesView(APIView):
    """GET the form (definitions + current values) · PUT {values: {slug: value}}."""
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG)]

    def _product(self, request, pk):
        vendor = get_current_vendor(request.user)
        return get_object_or_404(Product.objects.select_related('sub_category'), pk=pk, vendor=vendor)

    def _payload(self, product):
        defs = services.definitions_for(product.sub_category)
        return {
            'definitions': [services.definition_json(d) for d in defs],
            'values': services.values_for_edit(product),
            'missing_required': services.missing_required(product, defs),
        }

    def get(self, request, pk):
        return Response(self._payload(self._product(request, pk)))

    def put(self, request, pk):
        product = self._product(request, pk)
        values = request.data.get('values')
        if not isinstance(values, dict):
            return Response({'detail': 'Send {"values": {...}}.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            services.save_values(product, values)
        except services.AttributeValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self._payload(product))


class CompareView(APIView):
    """?ids=1,2,3 → side-by-side rows for up to four published products."""
    permission_classes = [AllowAny]

    def get(self, request):
        from core.service import get_exchange_rates
        from decimal import Decimal

        raw = (request.query_params.get('ids') or '').split(',')
        ids = list(dict.fromkeys(int(i) for i in raw if i.strip().isdigit()))[:MAX_COMPARE]
        if not ids:
            return Response({'products': [], 'rows': []})
        products = {p.pk: p for p in Product.published.filter(pk__in=ids).select_related('brand', 'vendor')}
        ordered = [products[i] for i in ids if i in products]
        specs = services.specs_for([p.pk for p in ordered])

        currency = request.headers.get('X-Currency', 'GHS') or 'GHS'
        rate = Decimal(str((get_exchange_rates() or {}).get(currency, 1)))

        def image(p):
            try:
                return p.image.url if p.image else ''
            except ValueError:
                return ''

        cols = [{
            'id': p.pk, 'title': p.title, 'sku': p.sku, 'slug': p.slug, 'image': image(p),
            'price': round(p.price * rate, 2), 'currency': currency,
            'rating': p.avg_rating, 'review_count': p.review_count,
            'brand': getattr(p.brand, 'title', '') or '', 'seller': getattr(p.vendor, 'name', '') or '',
        } for p in ordered]

        # Union of attribute names across products, in first-seen order.
        names, by_product = {}, {}
        for p in ordered:
            for row in specs.get(p.pk, []):
                names.setdefault(row['slug'], row['name'])
                by_product[(p.pk, row['slug'])] = row['value']
        rows = [{'slug': slug, 'name': name, 'values': [by_product.get((p.pk, slug), '') for p in ordered]}
                for slug, name in names.items()]
        for row in rows:
            vals = [v for v in row['values'] if v]
            row['differs'] = len(set(vals)) > 1 or len(vals) != len(row['values'])
        return Response({'products': cols, 'rows': rows})

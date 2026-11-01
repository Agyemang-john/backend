from decimal import Decimal

from django.core.cache import cache
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from product.models import Product, Variants
from vendor.access import Capability, get_current_vendor, require_capability

from . import services

OFFERS_CACHE_SECONDS = 60


def _offer_rows(item_id, rate):
    offers = list(Product.published.filter(catalog_item_id=item_id).select_related('vendor')
                  .only('pk', 'sku', 'slug', 'price', 'variant', 'total_quantity', 'product_type', 'fulfilment_type',
                        'sourcing_min_days', 'sourcing_max_days', 'avg_rating', 'review_count', 'is_buybox_winner',
                        'return_period_days', 'vendor__name', 'vendor__slug', 'vendor__handling_days',
                        'vendor__is_approved'))
    stock = dict(Variants.objects.filter(product__in=[p.pk for p in offers])
                 .values_list('product_id').annotate(t=Sum('quantity')))
    rows = []
    for p in offers:
        qty = (stock.get(p.pk, 0) if p.variant != 'None' else p.total_quantity) or 0
        rows.append({
            'product_id': p.pk, 'sku': p.sku, 'slug': p.slug,
            'price': round(p.price * rate, 2),
            'condition': p.product_type or 'new',
            'in_stock': qty > 0,
            'fulfilment_type': p.fulfilment_type,
            'handling_days': p.vendor.handling_days,
            'return_period_days': p.return_period_days,
            'rating': p.avg_rating, 'review_count': p.review_count,
            'seller': {'name': p.vendor.name, 'slug': p.vendor.slug},
            'is_featured': p.is_buybox_winner,
        })
    rows.sort(key=lambda r: (not r['is_featured'], not r['in_stock'], r['price']))
    return rows


class ProductOffersView(APIView):
    """Everyone selling the same item as this product: for "Other sellers" on the product page."""
    permission_classes = [AllowAny]

    def get(self, request, pk):
        from core.service import get_exchange_rates
        currency = request.headers.get('X-Currency', 'GHS') or 'GHS'
        key = f"catalog_offers:{pk}:{currency}"
        data = cache.get(key)
        if data is None:
            product = get_object_or_404(Product.objects.only('pk', 'catalog_item_id'), pk=pk)
            rate = Decimal(str((get_exchange_rates() or {}).get(currency, 1)))
            offers = _offer_rows(product.catalog_item_id, rate) if product.catalog_item_id else []
            data = {'currency': currency, 'catalog_item': product.catalog_item_id, 'offers': offers,
                    'offer_count': len(offers)}
            cache.set(key, data, OFFERS_CACHE_SECONDS)
        return Response(data)


class OfferSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.select_related('vendor'))
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    quantity = serializers.IntegerField(min_value=1, max_value=100000)
    condition = serializers.ChoiceField(choices=['new', 'used', 'refurbished'], default='new')
    seller_sku = serializers.CharField(max_length=64, required=False, allow_blank=True, default='')


class SellerOfferView(APIView):
    """POST: list an item that's already on the site, at your own price and stock."""
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG)]

    def get(self, request):
        """?product=<id> → can my store offer this item?"""
        vendor = get_current_vendor(request.user)
        product = get_object_or_404(Product.objects.only('pk', 'vendor_id', 'catalog_item_id', 'variant', 'status'),
                                    pk=request.query_params.get('product') or 0)
        mine = None
        if product.catalog_item_id:
            mine = Product.objects.filter(catalog_item_id=product.catalog_item_id, vendor=vendor).values('pk', 'sku', 'slug').first()
        if product.vendor_id == vendor.pk:
            mine = {'pk': product.pk}
        return Response({'can_offer': mine is None and product.variant == 'None' and product.status == 'published',
                         'has_variants': product.variant != 'None', 'my_offer': mine})

    def post(self, request):
        from payments.subscription_permissions import SubscriptionGateMixin

        vendor = get_current_vendor(request.user)
        ser = OfferSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        gate = SubscriptionGateMixin()
        gate.request = request
        gate._gate_product_limit()      # same plan limit as any new listing
        try:
            offer = services.create_offer(vendor, d['product'], price=d['price'], quantity=d['quantity'],
                                          condition=d['condition'], seller_sku=d['seller_sku'])
        except services.CatalogError as exc:
            return Response({'detail': str(exc)}, status=exc.status)
        from django.db.models import F
        from payments.models import SubscriptionUsage
        SubscriptionUsage.objects.filter(vendor=vendor).update(active_products_count=F('active_products_count') + 1)
        return Response({'id': offer.pk, 'sku': offer.sku, 'slug': offer.slug}, status=status.HTTP_201_CREATED)

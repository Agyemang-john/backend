from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from product.models import Product
from vendor.access import Capability, get_current_vendor, require_capability

from . import services
from .billing import card_paused, saved_card
from .models import AdCampaign, AdCampaignProduct, AdDailyStat, AdKeyword

MAX_PRODUCTS = 50
MAX_KEYWORDS = 100


class ClickView(APIView):
    """A shopper clicked a sponsored product. Token from the listing; billed at most once per visitor per 30 min."""
    permission_classes = [AllowAny]

    def post(self, request):
        result, _cost = services.record_click(request, str(request.data.get('token') or ''))
        return Response({'ok': result == 'billed' or result in ('duplicate', 'budget', 'bot')},
                        status=status.HTTP_400_BAD_REQUEST if result == 'invalid' else status.HTTP_200_OK)


class KeywordSerializer(serializers.Serializer):
    phrase = serializers.CharField(max_length=60)
    match = serializers.ChoiceField(choices=AdKeyword.MATCH_CHOICES, default=AdKeyword.PHRASE)
    bid = serializers.DecimalField(max_digits=8, decimal_places=2, required=False, allow_null=True, min_value=services.MIN_BID)


class CampaignSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    status = serializers.ChoiceField(choices=AdCampaign.STATUS_CHOICES, default=AdCampaign.ACTIVE)
    bid = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=services.MIN_BID)
    daily_budget = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('5.00'))
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False, allow_null=True)
    auto_targeting = serializers.BooleanField(default=True)
    products = serializers.ListField(child=serializers.IntegerField(), max_length=MAX_PRODUCTS, required=False)
    keywords = KeywordSerializer(many=True, required=False)

    def validate(self, attrs):
        start = attrs.get('start_date') or timezone.localdate()
        attrs['start_date'] = start
        if attrs.get('end_date') and attrs['end_date'] < start:
            raise serializers.ValidationError({'end_date': 'Ends before it starts.'})
        if attrs.get('daily_budget') is not None and attrs.get('bid') is not None and attrs['daily_budget'] < attrs['bid']:
            raise serializers.ValidationError({'daily_budget': 'Daily budget must cover at least one click.'})
        kws = attrs.get('keywords') or []
        if len(kws) > MAX_KEYWORDS:
            raise serializers.ValidationError({'keywords': f'Up to {MAX_KEYWORDS} keywords.'})
        for k in kws:
            k['phrase'] = services.normalise(k['phrase'])[:60]
        return attrs


def _campaign_json(c, stats=None):
    return {
        'id': c.pk, 'name': c.name, 'status': c.status, 'bid': c.bid, 'daily_budget': c.daily_budget,
        'start_date': c.start_date, 'end_date': c.end_date, 'auto_targeting': c.auto_targeting,
        'products': [{'id': i.product_id, 'title': i.product.title, 'sku': i.product.sku} for i in c.items.all()],
        'keywords': [{'phrase': k.phrase, 'match': k.match, 'bid': k.bid} for k in c.keywords.all()],
        'stats': stats or {},
    }


def _totals(qs):
    t = qs.aggregate(impressions=Sum('impressions'), clicks=Sum('clicks'), spend=Sum('spend'),
                     orders=Sum('orders'), sales=Sum('sales'))
    t = {k: v or 0 for k, v in t.items()}
    t['ctr'] = round(100 * t['clicks'] / t['impressions'], 2) if t['impressions'] else 0
    t['acos'] = round(100 * t['spend'] / t['sales'], 1) if t['sales'] else None   # ad cost of sales
    return t


class SellerCampaignsView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG)]

    def get(self, request):
        vendor = get_current_vendor(request.user)
        since = timezone.localdate() - timedelta(days=30)
        campaigns = AdCampaign.objects.filter(vendor=vendor).exclude(status=AdCampaign.ARCHIVED) \
            .prefetch_related('items__product', 'keywords').order_by('-created_at')
        return Response({
            'results': [_campaign_json(c, _totals(AdDailyStat.objects.filter(campaign=c, date__gte=since)))
                        for c in campaigns],
            'reserve_cpc': services.RESERVE_CPC,
            'funded': vendor.pk in services.refresh_funded_vendors() if campaigns else True,
            'has_card': bool(saved_card(vendor.pk)),
            'card_paused': card_paused(vendor.pk),
        })

    def post(self, request):
        vendor = get_current_vendor(request.user)
        ser = CampaignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            c = AdCampaign.objects.create(vendor=vendor, **{k: v for k, v in ser.validated_data.items()
                                                            if k not in ('products', 'keywords')})
            err = _set_children(c, vendor, ser.validated_data)
            if err:
                transaction.set_rollback(True)
                return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_campaign_json(c), status=status.HTTP_201_CREATED)


def _set_children(c, vendor, data):
    if 'products' in data:
        ids = set(data['products'])
        own = set(Product.objects.filter(pk__in=ids, vendor=vendor).values_list('pk', flat=True))
        if ids - own:
            return "You can only advertise your own products."
        AdCampaignProduct.objects.filter(campaign=c).exclude(product_id__in=own).delete()
        existing = set(AdCampaignProduct.objects.filter(campaign=c).values_list('product_id', flat=True))
        AdCampaignProduct.objects.bulk_create([AdCampaignProduct(campaign=c, product_id=p) for p in own - existing])
    if 'keywords' in data:
        AdKeyword.objects.filter(campaign=c).delete()
        seen = {}
        for k in data['keywords']:
            if k['phrase']:
                seen[k['phrase']] = k
        AdKeyword.objects.bulk_create([AdKeyword(campaign=c, phrase=p, match=k['match'], bid=k.get('bid'))
                                       for p, k in seen.items()])
    return None


class SellerCampaignView(APIView):
    permission_classes = [IsAuthenticated, require_capability(Capability.MANAGE_CATALOG)]

    def _get(self, request, pk):
        return get_object_or_404(AdCampaign.objects.prefetch_related('items__product', 'keywords'),
                                 pk=pk, vendor=get_current_vendor(request.user))

    def get(self, request, pk):
        c = self._get(request, pk)
        days = min(max(int(request.query_params.get('days', 30) or 30), 1), 90)
        since = timezone.localdate() - timedelta(days=days - 1)
        qs = AdDailyStat.objects.filter(campaign=c, date__gte=since)
        daily = list(qs.values('date').annotate(impressions=Sum('impressions'), clicks=Sum('clicks'), spend=Sum('spend'),
                                                 orders=Sum('orders'), sales=Sum('sales')).order_by('date'))
        by_product = list(qs.values('product_id', 'product__title').annotate(
            impressions=Sum('impressions'), clicks=Sum('clicks'), spend=Sum('spend'), orders=Sum('orders'),
            sales=Sum('sales')).order_by('-spend'))
        data = _campaign_json(c, _totals(qs))
        data.update({'daily': daily, 'by_product': by_product,
                     'spent_today': services.spend_today([c.pk]).get(c.pk, Decimal('0'))})
        return Response(data)

    def patch(self, request, pk):
        c = self._get(request, pk)
        current = {'name': c.name, 'status': c.status, 'bid': c.bid, 'daily_budget': c.daily_budget,
                   'start_date': c.start_date, 'end_date': c.end_date, 'auto_targeting': c.auto_targeting}
        ser = CampaignSerializer(data={**current, **request.data})
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            for k in ('name', 'status', 'bid', 'daily_budget', 'end_date', 'auto_targeting'):
                if k in request.data:
                    setattr(c, k, ser.validated_data[k])
            c.save()
            err = _set_children(c, c.vendor, {k: v for k, v in ser.validated_data.items()
                                              if k in ('products', 'keywords') and k in request.data})
            if err:
                transaction.set_rollback(True)
                return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_campaign_json(self._get(request, pk)))

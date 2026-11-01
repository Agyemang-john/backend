from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from product.models import Product, Variants

from .alerts import live_state, watch
from .emails import read_opt_out_token
from .models import EmailOptOut, ProductAlert

MAX_ACTIVE_ALERTS = 300


class AlertCreateSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.filter(status='published'))
    variant = serializers.PrimaryKeyRelatedField(queryset=Variants.objects.all(), required=False, allow_null=True)
    kind = serializers.ChoiceField(choices=ProductAlert.KIND_CHOICES)
    target_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False,
                                            allow_null=True, min_value=Decimal('0.01'))

    def validate(self, attrs):
        variant = attrs.get('variant')
        if variant is not None and variant.product_id != attrs['product'].pk:
            raise serializers.ValidationError({'variant': "This option belongs to a different product."})
        if attrs['kind'] == ProductAlert.STOCK and attrs.get('target_price') is not None:
            raise serializers.ValidationError({'target_price': "Only price alerts take a target price."})
        return attrs


def _currency(request):
    """(code, rate from GHS) for the shopper's chosen currency. Alerts are stored in GHS."""
    from core.service import get_exchange_rates
    code = request.headers.get('X-Currency', 'GHS') or 'GHS'
    if code == 'GHS':
        return 'GHS', Decimal('1')
    rate = (get_exchange_rates() or {}).get(code)
    return (code, Decimal(str(rate))) if rate else ('GHS', Decimal('1'))


def _money(value, rate):
    return None if value is None else round(value * rate, 2)


def _serialize(alerts, request):
    alerts = list(alerts)
    currency, rate = _currency(request)
    state = live_state({(a.product_id, a.variant_id) for a in alerts})
    rows = []
    for a in alerts:
        item = state.get((a.product_id, a.variant_id))
        if item is None:
            continue
        p, v = item['product'], item['variant']
        try:
            image = p.image.url if p.image else ''
        except ValueError:
            image = ''
        rows.append({
            'id': a.pk,
            'kind': a.kind,
            'source': a.source,
            'product': {'id': p.pk, 'title': p.title, 'sku': p.sku, 'slug': p.slug, 'image': image},
            'variant': ({'id': v.pk, 'size': getattr(v.size, 'name', None), 'color': getattr(v.color, 'name', None)}
                        if v is not None else None),
            'currency': currency,
            'target_price': _money(a.target_price, rate),
            'reference_price': _money(a.reference_price, rate),
            'current_price': _money(item['price'], rate),
            'in_stock': item['in_stock'],
            'created_at': a.created_at,
        })
    return rows


class AlertListCreateView(APIView):
    """GET my active alerts · POST {product, variant?, kind, target_price?} to watch an item."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ProductAlert.objects.filter(user=request.user, is_active=True).order_by('-created_at')
        product_id = request.query_params.get('product')
        if product_id:
            if not str(product_id).isdigit():
                return Response({'detail': 'Bad product id.'}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(product_id=product_id)
        else:
            qs = qs[:MAX_ACTIVE_ALERTS]
        return Response({'results': _serialize(qs, request)})

    def post(self, request):
        ser = AlertCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if ProductAlert.objects.filter(user=request.user, is_active=True).count() >= MAX_ACTIVE_ALERTS:
            return Response({'detail': f"You can watch up to {MAX_ACTIVE_ALERTS} items. Remove some first."},
                            status=status.HTTP_400_BAD_REQUEST)
        target = data.get('target_price')
        if target is not None:
            _, rate = _currency(request)
            target = round(target / rate, 2)
        alert = watch(request.user, data['product'], data['kind'], variant=data.get('variant'),
                      target_price=target)
        return Response(_serialize([alert], request)[0], status=status.HTTP_201_CREATED)


class AlertDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        alert = get_object_or_404(ProductAlert, pk=pk, user=request.user)
        alert.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class EmailPreferencesView(APIView):
    """GET/PUT {"alerts": bool, "cart": bool} — true means "send me these"."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        off = set(EmailOptOut.objects.filter(user=request.user).values_list('category', flat=True))
        return Response({c: c not in off for c, _ in EmailOptOut.CATEGORY_CHOICES})

    def put(self, request):
        for category, _ in EmailOptOut.CATEGORY_CHOICES:
            if category not in request.data:
                continue
            wanted = request.data[category]
            if not isinstance(wanted, bool):
                return Response({category: 'Must be true or false.'}, status=status.HTTP_400_BAD_REQUEST)
            if wanted:
                EmailOptOut.objects.filter(user=request.user, category=category).delete()
            else:
                EmailOptOut.objects.get_or_create(user=request.user, category=category)
        return self.get(request)


class UnsubscribeView(APIView):
    """One-click unsubscribe from an email link. GET describes the token, POST applies it."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def _read(self, token):
        parsed = read_opt_out_token(token)
        if parsed is None:
            return None
        return parsed

    def get(self, request, token):
        parsed = self._read(token)
        if parsed is None:
            return Response({'detail': 'This link is not valid.'}, status=status.HTTP_400_BAD_REQUEST)
        _, category = parsed
        return Response({'category': category, 'label': dict(EmailOptOut.CATEGORY_CHOICES)[category]})

    def post(self, request, token):
        parsed = self._read(token)
        if parsed is None:
            return Response({'detail': 'This link is not valid.'}, status=status.HTTP_400_BAD_REQUEST)
        user_id, category = parsed
        from django.contrib.auth import get_user_model
        if get_user_model().objects.filter(pk=user_id).exists():
            EmailOptOut.objects.get_or_create(user_id=user_id, category=category)
        return Response({'category': category, 'label': dict(EmailOptOut.CATEGORY_CHOICES)[category],
                         'unsubscribed': True})

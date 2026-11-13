"""
vendor/coupon_views.py
Sellers making coupon codes for their own products (rules: order/coupons.py).

    GET/POST     coupons/        list / create           [plan: can_offer_discounts]
    PATCH/DELETE coupons/<id>/   edit / switch off / delete

A store coupon only takes money off that store's items, and the discount
comes out of the store's earnings (payments/ledger.py SELLER_DISCOUNT), so
the seller decides how generous it is. Guardrails:
  - letters, digits and dashes, 4–20 characters, unique across Negromart
  - MIN_PERCENT–MAX_PERCENT % off, or a fixed amount
  - at most MAX_DURATION_DAYS long, at most MAX_OPEN_COUPONS running or scheduled
  - optional product list must be the store's own products
  - once a shopper has used it, only the on/off switch can change (and the
    budget can go up) and it can't be deleted (orders and the ledger point at it)

Two kinds (Coupon.clippable):
  clip coupon    "Save 10% with coupon" badge on the products; shoppers clip it
                 and it applies at checkout. No code needed (one is made up).
  private code   only shoppers the seller gives the code to can use it.
Optional budget: the coupon stops once that much discount has been given.

Access: active membership in an approved store with the catalog capability.
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, OuterRef, Subquery, Sum
from django.utils.crypto import get_random_string
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from payments.subscription_permissions import require_feature
from product.models import ClippedCoupon, Coupon, Product

from .access import Capability, get_membership, require_capability

MIN_PERCENT = 5
MAX_PERCENT = 90
MAX_DURATION_DAYS = 90
MAX_OPEN_COUPONS = 20
CODE_RE = re.compile(r'^[A-Z0-9-]{4,20}$')
START_GRACE = timedelta(minutes=10)


def _vendor(request):
    return get_membership(request.user).vendor


def _status(coupon, now):
    if not coupon.active:
        return 'paused' if coupon.valid_to > now else 'ended'
    if coupon.valid_to < now:
        return 'ended'
    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        return 'used_up'
    if coupon.budget is not None and coupon.budget_left <= 0:
        return 'used_up'
    if coupon.valid_from > now:
        return 'scheduled'
    return 'live'


class SellerCouponSerializer(serializers.ModelSerializer):
    discount_type = serializers.ChoiceField(choices=['percent', 'amount'], write_only=True)
    value = serializers.DecimalField(max_digits=10, decimal_places=2, write_only=True)
    products = serializers.PrimaryKeyRelatedField(
        source='applicable_products', queryset=Product.objects.all(), many=True, required=False,
    )
    product_titles = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    orders = serializers.SerializerMethodField()
    total_discount = serializers.SerializerMethodField()
    clips = serializers.SerializerMethodField()
    code = serializers.CharField(max_length=50, required=False, allow_blank=True)

    class Meta:
        model = Coupon
        fields = [
            'id', 'code', 'discount_type', 'value', 'discount_amount', 'discount_percentage',
            'max_discount_amount', 'min_purchase_amount', 'valid_from', 'valid_to',
            'max_uses', 'per_user_limit', 'used_count', 'products', 'product_titles',
            'active', 'status', 'orders', 'total_discount', 'created_at',
            'clippable', 'budget', 'discount_spent', 'clips',
        ]
        read_only_fields = ['discount_amount', 'discount_percentage', 'used_count', 'created_at',
                            'discount_spent']

    def get_product_titles(self, obj):
        return [p.title for p in obj.applicable_products.all()]

    def get_status(self, obj):
        return _status(obj, timezone.now())

    def get_orders(self, obj):
        return getattr(obj, 'n_orders', None) or 0

    def get_clips(self, obj):
        return getattr(obj, 'n_clips', None) or 0

    def get_total_discount(self, obj):
        return str(getattr(obj, 'sum_discount', None) or Decimal('0.00'))

    def validate(self, attrs):
        vendor = self.context['vendor']
        now = timezone.now()
        instance = self.instance

        # Shoppers have used it: it's a record of what they were offered.
        if instance and instance.redemptions.exists():
            locked = set(attrs) - {'active', 'budget'}
            if locked:
                raise serializers.ValidationError(
                    "Shoppers have already used this coupon. You can only switch it off or on, "
                    "or raise its budget; create a new coupon for different terms."
                )
            if 'budget' in attrs:
                old = instance.budget
                if attrs['budget'] is None or (old is not None and attrs['budget'] < old):
                    raise serializers.ValidationError({'budget': "You can only raise the budget now."})
            return attrs

        clippable = attrs.get('clippable', instance.clippable if instance else False)
        if not (attrs.get('code') or '').strip() and 'code' in attrs and instance is None:
            attrs.pop('code')
        if 'code' not in attrs and instance is None and clippable:
            # Clip coupons need no code from the seller; make an unguessable one.
            while True:
                code = f"CLIP-{get_random_string(8, 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789')}"
                if not Coupon.objects.filter(code=code).exists():
                    attrs['code'] = code
                    break
        elif 'code' in attrs:
            code = (attrs['code'] or '').strip().upper()
            if not CODE_RE.match(code):
                raise serializers.ValidationError(
                    {'code': "Use 4–20 letters, numbers or dashes (e.g. SUMMER-10)."}
                )
            taken = Coupon.objects.filter(code=code).exclude(pk=instance.pk if instance else None)
            if taken.exists():
                raise serializers.ValidationError({'code': "That code is already taken. Try another."})
            attrs['code'] = code
        elif not instance:
            raise serializers.ValidationError({'code': "Enter a code."})

        kind = attrs.pop('discount_type', None)
        value = attrs.pop('value', None)
        if instance is None and (kind is None or value is None):
            raise serializers.ValidationError({'value': "Enter how much the coupon takes off."})
        if kind is not None or value is not None:
            if kind is None or value is None:
                raise serializers.ValidationError({'value': "Send both discount_type and value."})
            if kind == 'percent':
                if not MIN_PERCENT <= value <= MAX_PERCENT:
                    raise serializers.ValidationError(
                        {'value': f"Between {MIN_PERCENT}% and {MAX_PERCENT}% off."}
                    )
                attrs['discount_percentage'] = float(value)
                attrs['discount_amount'] = None
            else:
                if value < 1:
                    raise serializers.ValidationError({'value': "At least GHS 1.00 off."})
                attrs['discount_amount'] = value
                attrs['discount_percentage'] = None
                attrs['max_discount_amount'] = None

        minimum = attrs.get('min_purchase_amount')
        amount_off = attrs.get('discount_amount', instance.discount_amount if instance else None)
        if minimum is not None and minimum < 0:
            raise serializers.ValidationError({'min_purchase_amount': "Can't be negative."})
        if amount_off and minimum is not None and minimum and amount_off >= minimum:
            raise serializers.ValidationError(
                {'min_purchase_amount': "Set the minimum spend above the amount off."}
            )

        start = attrs.get('valid_from', instance.valid_from if instance else None)
        end = attrs.get('valid_to', instance.valid_to if instance else None)
        if not start or not end:
            raise serializers.ValidationError({'valid_to': "Choose when the coupon starts and ends."})
        if 'valid_from' in attrs and start < now - START_GRACE:
            raise serializers.ValidationError({'valid_from': "Start time can't be in the past."})
        if end <= start:
            raise serializers.ValidationError({'valid_to': "Must be after the start."})
        if end - start > timedelta(days=MAX_DURATION_DAYS):
            raise serializers.ValidationError(
                {'valid_to': f"A coupon can run for at most {MAX_DURATION_DAYS} days."}
            )

        budget = attrs.get('budget')
        if budget is not None and budget < 1:
            raise serializers.ValidationError({'budget': "At least GHS 1.00, or leave empty for no budget."})

        for field in ('max_uses', 'per_user_limit'):
            if attrs.get(field) is not None and attrs[field] < 1:
                raise serializers.ValidationError({field: "At least 1, or leave empty for no limit."})

        products = attrs.get('applicable_products')
        if products:
            if any(p.vendor_id != vendor.pk for p in products):
                raise serializers.ValidationError({'products': "Choose only your own products."})

        if not instance:
            open_coupons = Coupon.objects.filter(vendor=vendor, active=True, valid_to__gte=now)
            if open_coupons.count() >= MAX_OPEN_COUPONS:
                raise serializers.ValidationError(
                    f"You can have up to {MAX_OPEN_COUPONS} running or scheduled coupons at a time."
                )
        return attrs

    def create(self, validated_data):
        validated_data['vendor'] = self.context['vendor']
        return super().create(validated_data)


class CouponAccess:
    permission_classes = [
        IsAuthenticated,
        require_capability(Capability.MANAGE_CATALOG),
        require_feature('can_offer_discounts'),
    ]

    def get_queryset(self, request):
        return (
            Coupon.objects.filter(vendor=_vendor(request))
            .annotate(
                n_orders=Count('redemptions'), sum_discount=Sum('redemptions__amount'),
                # Subquery, not a second join: a join would multiply the sum above.
                n_clips=Subquery(
                    ClippedCoupon.objects.filter(coupon=OuterRef('pk')).values('coupon')
                    .annotate(n=Count('id')).values('n')[:1]
                ),
            )
            .prefetch_related('applicable_products')
        )

    def context(self, request):
        return {'request': request, 'vendor': _vendor(request)}


class SellerCouponListView(CouponAccess, APIView):

    def get(self, request):
        coupons = list(self.get_queryset(request).order_by('-created_at')[:200])
        state = request.query_params.get('status')
        if state:
            now = timezone.now()
            coupons = [c for c in coupons if _status(c, now) == state]
        return Response({
            'results': SellerCouponSerializer(coupons, many=True, context=self.context(request)).data,
            'limits': {
                'min_percent': MIN_PERCENT,
                'max_percent': MAX_PERCENT,
                'max_duration_days': MAX_DURATION_DAYS,
                'max_open_coupons': MAX_OPEN_COUPONS,
            },
        })

    def post(self, request):
        serializer = SellerCouponSerializer(data=request.data, context=self.context(request))
        serializer.is_valid(raise_exception=True)
        coupon = serializer.save()
        coupon = self.get_queryset(request).get(pk=coupon.pk)
        return Response(
            SellerCouponSerializer(coupon, context=self.context(request)).data,
            status=status.HTTP_201_CREATED,
        )


class SellerCouponDetailView(CouponAccess, APIView):

    def get(self, request, pk):
        coupon = get_object_or_404(self.get_queryset(request), pk=pk)
        return Response(SellerCouponSerializer(coupon, context=self.context(request)).data)

    def patch(self, request, pk):
        coupon = get_object_or_404(self.get_queryset(request), pk=pk)
        serializer = SellerCouponSerializer(coupon, data=request.data, partial=True, context=self.context(request))
        serializer.is_valid(raise_exception=True)
        serializer.save()
        coupon = self.get_queryset(request).get(pk=pk)
        return Response(SellerCouponSerializer(coupon, context=self.context(request)).data)

    def delete(self, request, pk):
        coupon = get_object_or_404(self.get_queryset(request), pk=pk)
        if coupon.redemptions.exists():
            return Response(
                {'detail': "Shoppers have used this coupon. Switch it off instead of deleting it."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        coupon.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

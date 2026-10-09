"""
vendor/flash_sale_views.py
Sellers running flash sales on their own products.

    GET/POST     flash-sales/              list / create          [plan: can_offer_discounts]
    GET/PATCH/   flash-sales/<id>/         detail / edit / delete [plan: can_offer_discounts]
      DELETE
    GET          flash-sales/products/     own published products + variants for the picker

Seller sales go live on their own at start_time (Negromart staff can still
switch any sale off in the admin). Guardrails, so a deal page stays credible:
  - only the store's own published products
  - at least MIN_DISCOUNT_PERCENT off the current price
  - at most MAX_DURATION_DAYS long, and at most MAX_OPEN_SALES running or scheduled
  - no two overlapping sales on the same product/variant
  - once a sale has started only its switch (is_active) can change; it can't be deleted
    after anything has sold, so order history keeps pointing at it.

Access: active membership in an approved store with the catalog capability.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from payments.subscription_permissions import require_feature
from product.models import FlashSale, Product, Variants

from .access import Capability, get_membership, require_capability

MIN_DISCOUNT_PERCENT = Decimal('5')
MAX_DURATION_DAYS = 14
MAX_OPEN_SALES = 10
# Small allowance so "start now" from a form filled a minute ago isn't rejected.
START_GRACE = timedelta(minutes=10)


def _vendor(request):
    return get_membership(request.user).vendor


def _status(sale, now):
    if not sale.is_active:
        return 'paused' if sale.end_time > now else 'ended'
    if sale.end_time < now:
        return 'ended'
    if sale.max_quantity is not None and sale.sold_count >= sale.max_quantity:
        return 'sold_out'
    if sale.start_time > now:
        return 'scheduled'
    return 'live'


def _image_url(request, field):
    if not field:
        return None
    try:
        return request.build_absolute_uri(field.url) if request else field.url
    except ValueError:
        return None


class SellerFlashSaleSerializer(serializers.ModelSerializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    variant = serializers.PrimaryKeyRelatedField(queryset=Variants.objects.all(), required=False, allow_null=True)
    original_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    product_title = serializers.SerializerMethodField()
    product_image = serializers.SerializerMethodField()
    variant_title = serializers.SerializerMethodField()
    discount_percentage = serializers.FloatField(read_only=True)
    stock_remaining = serializers.IntegerField(read_only=True, allow_null=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = FlashSale
        fields = [
            'id', 'product', 'variant', 'product_title', 'product_image', 'variant_title',
            'sale_price', 'original_price', 'discount_percentage',
            'start_time', 'end_time', 'max_quantity', 'sold_count', 'stock_remaining',
            'label', 'is_active', 'status', 'created_at',
        ]
        read_only_fields = ['sold_count', 'created_at']

    def get_product_title(self, obj):
        return obj.product.title if obj.product else 'Product no longer available'

    def get_product_image(self, obj):
        return _image_url(self.context.get('request'), obj.product.image if obj.product else None)

    def get_variant_title(self, obj):
        return obj.variant.title if obj.variant else None

    def get_status(self, obj):
        return _status(obj, timezone.now())

    def validate(self, attrs):
        vendor = self.context['vendor']
        now = timezone.now()
        instance = self.instance

        # A running or finished sale is a record of what customers were offered:
        # only allow switching it on/off.
        if instance and instance.start_time <= now:
            locked = set(attrs) - {'is_active'}
            if locked:
                raise serializers.ValidationError(
                    "This sale has already started. You can only pause or end it; "
                    "create a new sale for different terms."
                )
            return attrs

        product = attrs.get('product', instance.product if instance else None)
        variant = attrs.get('variant', instance.variant if instance else None)
        if product is None or product.vendor_id != vendor.pk:
            raise serializers.ValidationError({'product': "Choose one of your own products."})
        if product.status != 'published':
            raise serializers.ValidationError({'product': "Only published products can go on sale."})

        # Default the "was" price to today's price, and never let a seller
        # inflate it to make the discount look bigger.
        current = variant.price if variant else product.price
        original = attrs.get('original_price')
        if original is None:
            original = current
        if original > current:
            raise serializers.ValidationError(
                {'original_price': f"Can't be higher than the current price ({current})."}
            )
        attrs['original_price'] = original

        sale_price = attrs.get('sale_price', instance.sale_price if instance else None)
        if sale_price is not None and original:
            max_price = (original * (1 - MIN_DISCOUNT_PERCENT / 100)).quantize(Decimal('0.01'))
            if sale_price > max_price:
                raise serializers.ValidationError({
                    'sale_price': f"Flash sales need at least {MIN_DISCOUNT_PERCENT:g}% off — "
                                  f"{max_price} or less."
                })

        start = attrs.get('start_time', instance.start_time if instance else None)
        end = attrs.get('end_time', instance.end_time if instance else None)
        if start and start < now - START_GRACE:
            raise serializers.ValidationError({'start_time': "Start time can't be in the past."})
        if start and end and end - start > timedelta(days=MAX_DURATION_DAYS):
            raise serializers.ValidationError(
                {'end_time': f"A flash sale can run for at most {MAX_DURATION_DAYS} days."}
            )

        # Shared model rules (variant matches product, sale < original, end > start, ...)
        candidate = FlashSale(
            product=product, variant=variant, sale_price=sale_price, original_price=original,
            start_time=start, end_time=end,
            max_quantity=attrs.get('max_quantity', instance.max_quantity if instance else None),
            sold_count=instance.sold_count if instance else 0,
            created_by=vendor,
        )
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)

        open_sales = FlashSale.objects.filter(
            product__vendor=vendor, is_active=True, end_time__gte=now,
        ).exclude(pk=instance.pk if instance else None)

        if start and end:
            overlap = open_sales.filter(
                product=product, start_time__lt=end, end_time__gt=start,
            ).filter(Q(variant=variant) if variant else Q(variant__isnull=True))
            if overlap.exists():
                raise serializers.ValidationError(
                    "This product already has a flash sale during those dates."
                )

        if not instance and open_sales.count() >= MAX_OPEN_SALES:
            raise serializers.ValidationError(
                f"You can have up to {MAX_OPEN_SALES} running or scheduled flash sales at a time."
            )
        return attrs

    def create(self, validated_data):
        validated_data['created_by'] = self.context['vendor']
        return super().create(validated_data)


class FlashSaleAccess:
    permission_classes = [
        IsAuthenticated,
        require_capability(Capability.MANAGE_CATALOG),
        require_feature('can_offer_discounts'),
    ]

    def get_queryset(self, request):
        return (
            FlashSale.objects
            .filter(product__vendor=_vendor(request))
            .select_related('product', 'variant')
        )

    def context(self, request):
        return {'request': request, 'vendor': _vendor(request)}


class SellerFlashSaleListView(FlashSaleAccess, APIView):

    def get(self, request):
        now = timezone.now()
        sales = list(self.get_queryset(request).order_by('-start_time')[:200])
        state = request.query_params.get('status')
        if state:
            sales = [s for s in sales if _status(s, now) == state]
        data = SellerFlashSaleSerializer(sales, many=True, context=self.context(request)).data
        return Response({
            'results': data,
            'limits': {
                'min_discount_percent': float(MIN_DISCOUNT_PERCENT),
                'max_duration_days': MAX_DURATION_DAYS,
                'max_open_sales': MAX_OPEN_SALES,
            },
        })

    def post(self, request):
        serializer = SellerFlashSaleSerializer(data=request.data, context=self.context(request))
        serializer.is_valid(raise_exception=True)
        sale = serializer.save()
        return Response(
            SellerFlashSaleSerializer(sale, context=self.context(request)).data,
            status=status.HTTP_201_CREATED,
        )


class SellerFlashSaleDetailView(FlashSaleAccess, APIView):

    def get(self, request, pk):
        sale = get_object_or_404(self.get_queryset(request), pk=pk)
        return Response(SellerFlashSaleSerializer(sale, context=self.context(request)).data)

    def patch(self, request, pk):
        sale = get_object_or_404(self.get_queryset(request), pk=pk)
        serializer = SellerFlashSaleSerializer(sale, data=request.data, partial=True, context=self.context(request))
        serializer.is_valid(raise_exception=True)
        sale = serializer.save()
        return Response(SellerFlashSaleSerializer(sale, context=self.context(request)).data)

    def delete(self, request, pk):
        sale = get_object_or_404(self.get_queryset(request), pk=pk)
        if sale.sold_count or sale.start_time <= timezone.now():
            return Response(
                {'detail': "This sale has already started. End it instead of deleting it."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        sale.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SellerFlashSaleProductsView(FlashSaleAccess, APIView):
    """Own published products (+ variants) for the create form's picker."""

    def get(self, request):
        products = (
            Product.objects
            .filter(vendor=_vendor(request), status='published')
            .prefetch_related('variants')
            .order_by('title')
        )
        q = (request.query_params.get('q') or '').strip()
        if q:
            products = products.filter(title__icontains=q)
        return Response([
            {
                'id': p.id,
                'title': p.title,
                'price': p.price,
                'image': _image_url(request, p.image),
                'variants': [
                    {'id': v.id, 'title': v.title, 'price': v.price}
                    for v in p.variants.all()
                ],
            }
            for p in products[:50]
        ])

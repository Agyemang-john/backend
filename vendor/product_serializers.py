from product import sourcing as product_sourcing
from product import variations
import re
from django.db import transaction
from rest_framework import serializers
from django.utils.text import slugify
from product.models import Product, ProductImages, Variants, VariantImage, ProductDeliveryOption, DeliveryOption, Sub_Category, Brand, Color, Size
from address.models import Country
from decimal import Decimal
import json

# Helper serializers
class SubCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Sub_Category
        fields = ['id', 'title', 'slug']

class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ['id', 'title', 'slug']

class ColorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Color
        fields = ['id', 'name', 'code']

class SizeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Size
        fields = ['id', 'name', 'code']

class DeliveryOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryOption
        fields = ['id', 'name', 'description', 'min_days', 'max_days', 'cost']

class ProductImagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImages
        fields = ['id', 'images']

class VariantImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = VariantImage
        fields = ['id', 'images', 'position']


class VariantsSerializer(serializers.ModelSerializer):
    color = serializers.PrimaryKeyRelatedField(queryset=Color.objects.all(), allow_null=True, required=False)
    size = serializers.PrimaryKeyRelatedField(queryset=Size.objects.all(), allow_null=True, required=False)
    image = serializers.ImageField(required=False, allow_null=True)
    # Extra photos are uploaded one by one to vendor/variants/<id>/images/
    # (vendor/variant_gallery.py); a product save only removes or reorders them.
    gallery = VariantImageSerializer(many=True, read_only=True, source='variantimage_set')
    # {option key: value}; cleaned against the product's options by ProductSerializer.
    options = serializers.JSONField(required=False)

    def to_internal_value(self, data):
        # Remove 'image' from data to prevent validation errors
        data = data.copy()
        data.pop('image', None)
        return super().to_internal_value(data)

    class Meta:
        model = Variants
        fields = ['id', 'title', 'size', 'color', 'options', 'image', 'quantity', 'price', 'unit_cost', 'seller_sku', 'gallery',
                  'fulfilment_type', 'sourcing_min_days', 'sourcing_max_days']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['options'] = variations.variant_options(instance)   # also for older size/colour variants
        return data

    def validate(self, attrs):
        # Blank fulfilment_type: the variant follows its product.
        kind = attrs.get('fulfilment_type', '')
        if kind:
            error = product_sourcing.validate(kind, attrs.get('sourcing_min_days', 0), attrs.get('sourcing_max_days', 0))
            if error:
                raise serializers.ValidationError({'sourcing_max_days': error})
        if kind in ('', product_sourcing.IN_STOCK):
            attrs['sourcing_min_days'] = attrs['sourcing_max_days'] = 0
        return attrs

    def create(self, validated_data):
        variant = Variants(**validated_data)
        if 'options' in validated_data:
            variations.mirror_foreign_keys(variant)
        variant.save()
        return variant

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if 'options' in validated_data:
            variations.mirror_foreign_keys(instance)
        elif 'size' in validated_data or 'color' in validated_data:
            instance.options = {}   # an older client set size/colour: save() rebuilds options from them
        instance.save()
        return instance

class ProductDeliveryOptionSerializer(serializers.ModelSerializer):
    deliveryOptionId = serializers.PrimaryKeyRelatedField(
        queryset=DeliveryOption.objects.all(),
        source="delivery_option",
        write_only=True
    )
    delivery_option = DeliveryOptionSerializer(read_only=True)

    class Meta:
        model = ProductDeliveryOption
        fields = ['id', 'deliveryOptionId', 'delivery_option', 'default', 'variant']

    def create(self, validated_data):
        return ProductDeliveryOption.objects.create(**validated_data)

class ProductSerializer(serializers.ModelSerializer):
    sub_category = serializers.PrimaryKeyRelatedField(queryset=Sub_Category.objects.all(), allow_null=True)
    brand = serializers.PrimaryKeyRelatedField(queryset=Brand.objects.all(), allow_null=True)
    available_in_regions = serializers.PrimaryKeyRelatedField(many=True, queryset=Country.objects.all(), required=False)
    delivery_options = ProductDeliveryOptionSerializer(many=True, source='productdeliveryoption_set', required=False)
    p_images = ProductImagesSerializer(many=True, read_only=True)
    variants = VariantsSerializer(many=True, required=False)   
    image = serializers.ImageField(required=False, allow_null=True)
    video = serializers.FileField(required=False, allow_null=True)

    class Meta:
        model = Product
        fields = [
            'id', 'slug', 'sub_category', 'vendor', 'variant', 'brand', 'status', 'title',
            'image', 'video', 'price', 'old_price', 'features', 'description', 'specifications',
            'delivery_returns', 'available_in_regions', 'product_type', 'total_quantity',
            'weight', 'volume', 'life', 'mfd', 'return_period_days', 'warranty_period_days',
            'trending_score', 'deals_of_the_day', 'recommended_for_you', 'popular_product',
            'delivery_options', 'sku', 'seller_sku', 'review_note', 'date', 'updated', 'views',
            'p_images', 'variants',
            'fulfilment_type', 'sourcing_min_days', 'sourcing_max_days', 'sourced_from',
            'gtin', 'variation_axes', 'unit_cost',
        ]
        # review_note is written by Negromart reviewers (e.g. why it was rejected).
        # variation_axes is read from the raw request (JSON in multipart) in validate().
        # status is Negromart's review decision (a seller can't publish their own product);
        # a draft moves to review on its own once it has a photo (update()).
        read_only_fields = ['vendor', 'sku', 'review_note', 'date', 'updated', 'views', 'variation_axes', 'status']

    PRODUCT_VIDEO_MAX_BYTES = 50 * 1024 * 1024    # what the seller help page promises

    def validate_video(self, value):
        # MP4, WebM or MOV by content, stored under a generated name: Spaces
        # serves a file with the type its name implies (see review_media).
        if not value:
            return value
        from product.review_media import MediaError, check_video
        try:
            value.name = check_video(value, max_bytes=self.PRODUCT_VIDEO_MAX_BYTES)
        except MediaError as exc:
            raise serializers.ValidationError(str(exc))
        return value

    def validate_price(self, value):
        if value <= 0:
            raise serializers.ValidationError("Price must be a positive value.")
        return value

    def validate_available_in_regions(self, value):
        if len(value) < 1:
            raise serializers.ValidationError("At least one region must be selected.")
        return value

    def validate_title(self, value):
        # Titles are unique within a store (several stores may sell the same
        # item: catalog/ groups them as offers on one product page).
        value = ' '.join((value or '').split())
        request = self.context.get('request')
        vendor = getattr(request.user, 'current_vendor', None) if request else None
        if vendor is not None:
            clash = Product.objects.filter(vendor=vendor, title__iexact=value)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError("You already have a product with this title.")
        return value

    def validate_gtin(self, value):
        from catalog.services import clean_gtin
        value = (value or '').strip()
        if not value:
            return ''
        cleaned = clean_gtin(value)
        if cleaned is None:
            raise serializers.ValidationError("Enter a valid barcode (8, 12, 13 or 14 digits).")
        return cleaned

    def validate_seller_sku(self, value):
        value = (value or '').strip()
        if not value:
            return ''
        vendor = self.context['request'].user.current_vendor
        clash = Product.objects.filter(vendor=vendor, seller_sku__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("Another product in your store already uses this SKU.")
        return value

    def validate(self, attrs):
        # Sourcing (product/sourcing.py): check against what is saved for any
        # field a partial update leaves out.
        def current(name, default):
            return attrs.get(name, getattr(self.instance, name, default) if self.instance else default)
        kind = current('fulfilment_type', product_sourcing.IN_STOCK)
        error = product_sourcing.validate(kind, current('sourcing_min_days', 0), current('sourcing_max_days', 0))
        if error:
            raise serializers.ValidationError({'sourcing_max_days': error})
        if kind == product_sourcing.IN_STOCK:
            attrs.update(sourcing_min_days=0, sourcing_max_days=0, sourced_from=None)

        # Variants arrive as a JSON string in multipart requests, outside the
        # nested serializer; check their SKUs here, before anything is saved.
        raw = self.context['request'].data.get('variants')
        if raw:
            try:
                variants_data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                variants_data = []
            self._check_variant_skus(variants_data, product=self.instance)
            self._check_variant_options(variants_data)
        return attrs

    def _requested_axes(self):
        """
        The seller's chosen options (Colour, Flavour...) or None when the client
        did not send any (older clients and bulk upload use `variant` alone).
        """
        if hasattr(self, '_axes'):
            return self._axes
        raw = self.context['request'].data.get('variation_axes')
        if raw is None:
            self._axes = None
            return None
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            self._axes = variations.clean_axes(parsed)
        except json.JSONDecodeError:
            raise serializers.ValidationError({'variation_axes': 'Variant options could not be read.'})
        except variations.VariationError as exc:
            raise serializers.ValidationError({'variation_axes': str(exc)})
        return self._axes

    def _check_variant_options(self, variants_data):
        """Every variant has a value for every option, and no two are the same version."""
        axes = self._requested_axes()
        if not axes:
            return
        seen = {}
        for i, var_data in enumerate(variants_data or [], start=1):
            if not isinstance(var_data, dict):
                continue
            try:
                options = variations.clean_variant_options(axes, var_data.get('options'), position=i)
            except variations.VariationError as exc:
                raise serializers.ValidationError({'variants': str(exc)})
            key = variations.combo_key(options)
            if key in seen:
                same = ' / '.join(options.values())
                raise serializers.ValidationError({'variants': (
                    f"Variants {seen[key]} and {i} are the same ({same}). Change one or remove it.")})
            seen[key] = i
            var_data['options'] = options          # cleaned, saved by VariantsSerializer
            var_data.pop('size', None)              # derived from options instead
            var_data.pop('color', None)

    def _apply_axes(self, validated_data, instance=None):
        axes = self._requested_axes()
        if axes is not None:
            validated_data['variation_axes'] = axes
            validated_data['variant'] = variations.variant_type_for(axes)
        elif instance is not None and validated_data.get('variant', instance.variant) != instance.variant:
            # An older client switched Size/Color: drop options it can't see.
            validated_data['variation_axes'] = []

    def _parsed_variants(self, variants_raw):
        """The variants list, with options cleaned against the chosen options."""
        if not variants_raw:
            return []
        try:
            data = json.loads(variants_raw) if isinstance(variants_raw, str) else variants_raw
        except json.JSONDecodeError:
            return []
        self._check_variant_options(data)
        return data

    def _check_variant_skus(self, variants_data, product=None):
        """Variant SKUs must be unique within the store (across all products)."""
        skus = [str(v.get('seller_sku') or '').strip() for v in variants_data or []]
        skus = [s for s in skus if s]
        if not skus:
            return
        lowered = [s.lower() for s in skus]
        if len(set(lowered)) != len(lowered):
            raise serializers.ValidationError({'variants': "Each variant needs a different SKU."})
        vendor = self.context['request'].user.current_vendor
        taken = Variants.objects.filter(product__vendor=vendor, seller_sku__iregex=r'^(' + '|'.join(map(re.escape, skus)) + r')$')
        if product is not None:
            taken = taken.exclude(product=product)
        if taken.exists():
            raise serializers.ValidationError({'variants': f"SKU {taken.first().seller_sku} is already used by another product."})

    def _remember_variant(self, var_data, variant):
        # The client tags each variant with a client_key so that, after saving,
        # it knows which new variant id to upload that variant's gallery to.
        key = var_data.get('client_key')
        if key:
            self._variant_keys[str(key)] = variant.id

    @staticmethod
    def _sync_gallery(variant, var_data):
        """
        Apply removals and the new order from var_data['keep_gallery']: the
        variant's photos in display order, existing ones by id and photos still
        to be uploaded as null (they are uploaded with their position).
        """
        order = var_data.get('keep_gallery')
        if not isinstance(order, list):
            return
        positions = {int(i): pos for pos, i in enumerate(order) if str(i).isdigit()}
        # Queryset delete still fires post_delete, which removes the files from storage.
        variant.variantimage_set.exclude(id__in=positions).delete()
        kept = list(variant.variantimage_set.all())
        for img in kept:
            img.position = positions[img.id]
        VariantImage.objects.bulk_update(kept, ['position'])

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['variation_axes'] = variations.effective_axes(instance)   # older Size/Color products too
        if getattr(self, '_variant_keys', None):
            data['variant_keys'] = self._variant_keys
        return data

    def _save_attributes(self, product, category_changed=False):
        """
        Structured specs (attributes/) sent with the product as `attributes`,
        a JSON {slug: value} for the product's category. Saved in the same
        transaction, so a product is never left half-created by a bad value.
        """
        from attributes import services as attribute_services

        if category_changed:
            attribute_services.prune_values(product)
        raw = self.context['request'].data.get('attributes')
        if raw in (None, ''):
            return
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            raise serializers.ValidationError({'attributes': 'Specifications could not be read.'})
        if not isinstance(values, dict):
            raise serializers.ValidationError({'attributes': 'Specifications could not be read.'})
        if not values:
            return
        try:
            attribute_services.save_values(product, values)
        except attribute_services.AttributeValueError as exc:
            raise serializers.ValidationError({'attributes': str(exc)})

    @transaction.atomic
    def create(self, validated_data):
        request = self.context['request']
        self._variant_keys = {}
        delivery_options_raw = request.data.get("delivery_options")
        variants_raw = request.data.get("variants")

        regions = validated_data.pop('available_in_regions', [])
        validated_data['slug'] = slugify(validated_data['title'])
        self._apply_axes(validated_data)
        product = Product.objects.create(**validated_data)
        product.available_in_regions.set(regions)

        # Handle new images from images[]
        new_images = request.FILES.getlist('images[]')
        for file in new_images:
            ProductImages.objects.create(
                product=product,
                images=file
            )

        # Handle variants
        if variants_raw:
            variants_data = self._parsed_variants(variants_raw)

            for i, var_data in enumerate(variants_data):
                var_serializer = VariantsSerializer(data=var_data)
                if var_serializer.is_valid(raise_exception=True):
                    variant = var_serializer.save(product=product)
                    self._remember_variant(var_data, variant)
                    image_key = f"variant_image_{i}"
                    if image_key in request.FILES:
                        variant.image = request.FILES[image_key]
                        variant.save()

        # Delivery options
        delivery_options_data = None
        if delivery_options_raw:
            try:
                if isinstance(delivery_options_raw, str):
                    delivery_options_data = json.loads(delivery_options_raw)
                else:
                    delivery_options_data = delivery_options_raw
            except json.JSONDecodeError:
                delivery_options_data = []

        if delivery_options_data:
            for delivery_data in delivery_options_data:
                delivery_serializer = ProductDeliveryOptionSerializer(data=delivery_data)
                delivery_serializer.is_valid(raise_exception=True)
                delivery_serializer.save(product=product)

        self._save_attributes(product)
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        request = self.context['request']
        self._variant_keys = {}
        category_changed = ('sub_category' in validated_data
                            and validated_data['sub_category'] != instance.sub_category)
        delivery_options_raw = request.data.get("delivery_options")
        variants_raw = request.data.get("variants")
        validated_data.pop('delivery_options', None)
        regions = validated_data.pop('available_in_regions', None)
        variants_data = validated_data.pop('variants', None)

        keep_images = []
        if 'keep_images' in self.context['request'].POST:
            try:
                keep_images = json.loads(self.context['request'].POST['keep_images'])
            except json.JSONDecodeError:
                pass

        if 'title' in validated_data:
            validated_data['slug'] = slugify(validated_data['title'])
        self._apply_axes(validated_data, instance)

        # Update product fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if regions is not None:
            instance.available_in_regions.set(regions)

        # Handle product images
        if keep_images:
            instance.p_images.exclude(id__in=keep_images).delete()

        new_images = self.context['request'].FILES.getlist('images[]')
        for file in new_images:
            ProductImages.objects.create(
                product=instance,
                images=file
            )

        # Handle variants
        if variants_raw:
            variants_data = self._parsed_variants(variants_raw)

            keep_variant_ids = [v.get('id') for v in variants_data if v.get('id')]
            instance.variants.exclude(id__in=keep_variant_ids).delete()

            for i, var_data in enumerate(variants_data):
                var_id = var_data.get('id')
                if var_id:
                    try:
                        variant_instance = instance.variants.get(id=var_id)
                        var_serializer = VariantsSerializer(variant_instance, data=var_data, partial=True)
                        if var_serializer.is_valid(raise_exception=True):
                            var_serializer.save()
                            self._sync_gallery(variant_instance, var_data)
                            self._remember_variant(var_data, variant_instance)
                            image_key = f"variant_image_{i}"
                            if image_key in request.FILES:
                                variant_instance.image = request.FILES[image_key]
                                variant_instance.save()
                    except Variants.DoesNotExist:
                        pass
                else:
                    var_serializer = VariantsSerializer(data=var_data)
                    if var_serializer.is_valid(raise_exception=True):
                        variant = var_serializer.save(product=instance)
                        self._remember_variant(var_data, variant)
                        image_key = f"variant_image_{i}"
                        if image_key in request.FILES:
                            variant.image = request.FILES[image_key]
                            variant.save()

        # Handle delivery options
        delivery_options_data = None
        if delivery_options_raw:
            try:
                if isinstance(delivery_options_raw, str):
                    delivery_options_data = json.loads(delivery_options_raw)
                else:
                    delivery_options_data = delivery_options_raw
            except json.JSONDecodeError:
                delivery_options_data = []

        if delivery_options_data is not None:
            keep_ids = []
            for delivery_data in delivery_options_data:
                delivery_id = delivery_data.get("id", None)
                if delivery_id:
                    try:
                        obj = instance.productdeliveryoption_set.get(id=delivery_id)
                        serializer = ProductDeliveryOptionSerializer(obj, data=delivery_data, partial=True)
                        serializer.is_valid(raise_exception=True)
                        serializer.save()
                        keep_ids.append(obj.id)
                    except ProductDeliveryOption.DoesNotExist:
                        pass
                else:
                    serializer = ProductDeliveryOptionSerializer(data=delivery_data)
                    serializer.is_valid(raise_exception=True)
                    new_obj = serializer.save(product=instance)
                    keep_ids.append(new_obj.id)

            instance.productdeliveryoption_set.exclude(id__in=keep_ids).delete()

        self._save_attributes(instance, category_changed=category_changed)

        # Drafts (e.g. from bulk upload without a photo) go to review once they have one.
        if instance.status == 'draft' and (instance.image or instance.variants.exclude(image='product.jpg').exists()):
            instance.status = 'in_review'
            instance.save(update_fields=['status'])
        return instance
 
from product.models import ProductReview
class ProductReviewSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source='product.title', read_only=True)
    product_image = serializers.ImageField(source='product.image', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True, allow_null=True)

    class Meta:
        model = ProductReview
        fields = [
            'id', 'product', 'product_title', 'product_image', 'user', 'user_email',
            'review', 'rating', 'status', 'seller_reply', 'seller_replied_at', 'date', 'updated'
        ]
        # Everything is read-only for sellers, including `status`: hiding a
        # review is a Negromart moderation decision. Sellers reply or report
        # instead (vendor/operations_views.py: ReviewReplyView, ReviewReportView).
        read_only_fields = fields

    def validate(self, data):
        request = self.context['request']
        vendor = request.user.current_vendor
        review = self.instance

        if review and review.vendor != vendor:
            raise serializers.ValidationError({"non_field_errors": "You can only update reviews for your own products."})

        return data

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        request = self.context.get('request')
        if representation['product_image'] and request:
            # Build absolute URL for product_image
            representation['product_image'] = request.build_absolute_uri(representation['product_image'])
        return representation


class VendorProductCardSerializer(serializers.ModelSerializer):
    """
    Ultra-light read-only serializer for the seller dashboard product grid.

    Only the columns the product card actually renders — no nested images,
    variants, delivery options, regions or rich-text fields — so a vendor's
    whole catalogue serializes fast even when there are thousands of products.
    Used for the GET (list) on ProductListCreateView; create/edit still use the
    full ProductSerializer.
    """
    class Meta:
        model = Product
        fields = ['id', 'title', 'slug', 'sku', 'seller_sku', 'image', 'status', 'review_note',
                  'total_quantity', 'price', 'old_price', 'views']


class VendorProductListSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for the seller store products list.
    Returns currency-converted prices based on the X-Currency request header.
    """
    sub_category = SubCategorySerializer(read_only=True)
    currency = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    old_price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ['id', 'title', 'slug', 'sku', 'image', 'sub_category',
                  'currency', 'price', 'old_price']

    def _get_currency(self, obj):
        request = self.context.get('request')
        return request.headers.get('X-Currency', 'GHS') if request else 'GHS'

    def _get_rate(self, currency):
        from core.service import get_exchange_rates
        rates = get_exchange_rates()
        return Decimal(str(rates.get(currency, 1)))

    def get_currency(self, obj):
        return self._get_currency(obj)

    def get_price(self, obj):
        currency = self._get_currency(obj)
        return round(obj.price * self._get_rate(currency), 2)

    def get_old_price(self, obj):
        if not obj.old_price:
            return None
        currency = self._get_currency(obj)
        return round(obj.old_price * self._get_rate(currency), 2)

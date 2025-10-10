from rest_framework import serializers
from django.utils.text import slugify
from product.models import Product, ProductImages, Variants, ProductDeliveryOption, DeliveryOption, Sub_Category, Brand, Color, Size
from address.models import Country
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

class VariantsSerializer(serializers.ModelSerializer):
    color = serializers.PrimaryKeyRelatedField(queryset=Color.objects.all(), allow_null=True, required=False)
    size = serializers.PrimaryKeyRelatedField(queryset=Size.objects.all(), allow_null=True, required=False)
    image = serializers.ImageField(required=False, allow_null=True)

    def to_internal_value(self, data):
        # Remove 'image' from data to prevent validation errors
        data = data.copy()
        data.pop('image', None)
        return super().to_internal_value(data)

    class Meta:
        model = Variants
        fields = ['id', 'title', 'size', 'color', 'image', 'quantity', 'price']

    def create(self, validated_data):
        variant = Variants.objects.create(**validated_data)
        return variant

    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
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
            'delivery_options', 'sku', 'date', 'updated', 'views', 'p_images', 'variants'
        ]
        read_only_fields = ['vendor', 'sku', 'date', 'updated', 'views']

    def validate_price(self, value):
        if value <= 0:
            raise serializers.ValidationError("Price must be a positive value.")
        return value

    def validate_available_in_regions(self, value):
        if len(value) < 1:
            raise serializers.ValidationError("At least one region must be selected.")
        return value

    def create(self, validated_data):
        request = self.context['request']
        delivery_options_raw = request.data.get("delivery_options")
        variants_raw = request.data.get("variants")

        regions = validated_data.pop('available_in_regions', [])
        validated_data['slug'] = slugify(validated_data['title'])
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
            try:
                if isinstance(variants_raw, str):
                    variants_data = json.loads(variants_raw)
                else:
                    variants_data = variants_raw
            except json.JSONDecodeError:
                variants_data = []

            for i, var_data in enumerate(variants_data):
                var_serializer = VariantsSerializer(data=var_data)
                if var_serializer.is_valid(raise_exception=True):
                    variant = var_serializer.save(product=product)
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

        return product

    def update(self, instance, validated_data):
        request = self.context['request']
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
            try:
                if isinstance(variants_raw, str):
                    variants_data = json.loads(variants_raw)
                else:
                    variants_data = variants_raw
            except json.JSONDecodeError:
                variants_data = []

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
            'review', 'rating', 'status', 'date', 'updated'
        ]
        read_only_fields = ['id', 'product', 'user', 'user_email', 'review', 'rating', 'date', 'updated', 'product_title', 'product_image']

    def validate(self, data):
        request = self.context['request']
        vendor = request.user.vendor_user
        review = self.instance

        if review and review.vendor != vendor:
            raise serializers.ValidationError({"non_field_errors": "You can only update reviews for your own products."})

        if 'status' in data and not isinstance(data['status'], bool):
            raise serializers.ValidationError({"status": "Status must be a boolean value (true/false)."})

        return data

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        request = self.context.get('request')
        if representation['product_image'] and request:
            # Build absolute URL for product_image
            representation['product_image'] = request.build_absolute_uri(representation['product_image'])
        return representation
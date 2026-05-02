"""
product/bulk_upload_serializer.py

Serializers for the bulk CSV/spreadsheet product upload feature.
Only vendors on the Pro plan or higher (can_access_bulk_upload=True) may use this.

Row schema matches the downloadable template:
  title, price, old_price, sub_category_slug, brand_slug, product_type,
  total_quantity, weight, volume, life, variant (None/Size/Color/Size-Color),
  description, features, specifications, delivery_returns,
  size_names (semicolon-separated), color_names (semicolon-separated),
  color_codes (semicolon-separated), variant_prices (semicolon-separated),
  variant_quantities (semicolon-separated)
"""

from rest_framework import serializers
from decimal import Decimal, InvalidOperation
from product.models import (
    Product, Sub_Category, Brand, Size, Color, Variants
)

REQUIRED_COLUMNS = [
    "title", "price", "old_price",
    "sub_category_slug", "product_type", "total_quantity",
]

VARIANT_CHOICES = {"None", "Size", "Color", "Size-Color"}
PRODUCT_TYPE_CHOICES = {"book", "grocery", "refurbished", "new", "used"}


class BulkProductRowSerializer(serializers.Serializer):
    """
    Validates and normalises a single CSV/sheet row into a dict ready for
    Product.objects.create(...).  Call .save(vendor) to persist.
    """

    # ── Required fields ───────────────────────────────────────────────────────
    title            = serializers.CharField(max_length=150)
    price            = serializers.DecimalField(max_digits=10, decimal_places=2)
    old_price        = serializers.DecimalField(max_digits=10, decimal_places=2)
    sub_category_slug = serializers.CharField()
    product_type     = serializers.ChoiceField(choices=list(PRODUCT_TYPE_CHOICES))
    total_quantity   = serializers.IntegerField(min_value=0)

    # ── Optional fields ───────────────────────────────────────────────────────
    brand_slug       = serializers.CharField(required=False, allow_blank=True, default="")
    weight           = serializers.FloatField(required=False, default=1.0)
    volume           = serializers.FloatField(required=False, default=1.0)
    life             = serializers.CharField(required=False, allow_blank=True, default="")
    variant          = serializers.ChoiceField(
        choices=list(VARIANT_CHOICES), required=False, default="None"
    )
    description      = serializers.CharField(required=False, allow_blank=True, default="")
    features         = serializers.CharField(required=False, allow_blank=True, default="")
    specifications   = serializers.CharField(required=False, allow_blank=True, default="")
    delivery_returns = serializers.CharField(required=False, allow_blank=True, default="")

    # ── Variant sub-fields (semicolon-separated strings) ──────────────────────
    size_names        = serializers.CharField(required=False, allow_blank=True, default="")
    color_names       = serializers.CharField(required=False, allow_blank=True, default="")
    color_codes       = serializers.CharField(required=False, allow_blank=True, default="")
    variant_prices    = serializers.CharField(required=False, allow_blank=True, default="")
    variant_quantities = serializers.CharField(required=False, allow_blank=True, default="")

    # ─────────────────────────────────────────────────────────────────────────
    # Cross-field validation
    # ─────────────────────────────────────────────────────────────────────────

    def validate_sub_category_slug(self, value):
        try:
            return Sub_Category.objects.get(slug=value)
        except Sub_Category.DoesNotExist:
            raise serializers.ValidationError(
                f"Sub-category '{value}' does not exist."
            )

    def validate_brand_slug(self, value):
        if not value:
            return None
        try:
            return Brand.objects.get(slug=value)
        except Brand.DoesNotExist:
            raise serializers.ValidationError(
                f"Brand '{value}' does not exist."
            )

    def validate(self, data):
        variant_type = data.get("variant", "None")

        # Validate variant sub-fields when variant type requires them
        if variant_type in ("Size", "Size-Color"):
            if not data.get("size_names"):
                raise serializers.ValidationError(
                    {"size_names": "size_names is required for Size/Size-Color variants."}
                )

        if variant_type in ("Color", "Size-Color"):
            if not data.get("color_names"):
                raise serializers.ValidationError(
                    {"color_names": "color_names is required for Color/Size-Color variants."}
                )

        # Parse semicolon-separated variant fields
        data["_sizes"]  = self._split(data.get("size_names", ""))
        data["_colors"] = self._split(data.get("color_names", ""))
        data["_codes"]  = self._split(data.get("color_codes", ""))
        data["_v_prices"] = self._split_decimals(data.get("variant_prices", ""))
        data["_v_qtys"]   = self._split_ints(data.get("variant_quantities", ""))

        return data

    # ─────────────────────────────────────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────────────────────────────────────

    def save(self, vendor):
        """Create product + variants. Returns created Product."""
        d = self.validated_data

        product = Product.objects.create(
            vendor=vendor,
            title=d["title"],
            price=d["price"],
            old_price=d["old_price"],
            sub_category=d["sub_category_slug"],   # already a model instance
            brand=d.get("brand_slug"),              # None or instance
            product_type=d["product_type"],
            total_quantity=d["total_quantity"],
            weight=d.get("weight", 1.0),
            volume=d.get("volume", 1.0),
            life=d.get("life", ""),
            variant=d.get("variant", "None"),
            description=d.get("description", ""),
            features=d.get("features", ""),
            specifications=d.get("specifications", ""),
            delivery_returns=d.get("delivery_returns", ""),
            status="in_review",
        )

        self._create_variants(product, d)
        return product

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _create_variants(self, product, d):
        variant_type = d.get("variant", "None")
        if variant_type == "None":
            return

        sizes  = d["_sizes"]
        colors = d["_colors"]
        codes  = d["_codes"]
        prices = d["_v_prices"]
        qtys   = d["_v_qtys"]

        def get_size(name):
            obj, _ = Size.objects.get_or_create(name=name)
            return obj

        def get_color(name, code=None):
            obj, _ = Color.objects.get_or_create(name=name, defaults={"code": code or ""})
            return obj

        idx = 0

        if variant_type == "Size":
            for i, size_name in enumerate(sizes):
                Variants.objects.create(
                    product=product,
                    size=get_size(size_name),
                    price=prices[i] if i < len(prices) else product.price,
                    quantity=qtys[i] if i < len(qtys) else product.total_quantity,
                )

        elif variant_type == "Color":
            for i, color_name in enumerate(colors):
                code = codes[i] if i < len(codes) else None
                Variants.objects.create(
                    product=product,
                    color=get_color(color_name, code),
                    price=prices[i] if i < len(prices) else product.price,
                    quantity=qtys[i] if i < len(qtys) else product.total_quantity,
                )

        elif variant_type == "Size-Color":
            for size_name in sizes:
                for j, color_name in enumerate(colors):
                    code = codes[j] if j < len(codes) else None
                    Variants.objects.create(
                        product=product,
                        size=get_size(size_name),
                        color=get_color(color_name, code),
                        price=prices[idx] if idx < len(prices) else product.price,
                        quantity=qtys[idx] if idx < len(qtys) else product.total_quantity,
                    )
                    idx += 1

    @staticmethod
    def _split(value: str) -> list[str]:
        return [v.strip() for v in value.split(";") if v.strip()]

    @staticmethod
    def _split_decimals(value: str) -> list[Decimal]:
        result = []
        for v in value.split(";"):
            v = v.strip()
            if v:
                try:
                    result.append(Decimal(v))
                except InvalidOperation:
                    pass
        return result

    @staticmethod
    def _split_ints(value: str) -> list[int]:
        result = []
        for v in value.split(";"):
            v = v.strip()
            if v:
                try:
                    result.append(int(v))
                except ValueError:
                    pass
        return result


class BulkUploadResultSerializer(serializers.Serializer):
    """Summary returned after a bulk upload attempt."""
    total_rows      = serializers.IntegerField()
    success_count   = serializers.IntegerField()
    failed_count    = serializers.IntegerField()
    created_product_ids = serializers.ListField(child=serializers.IntegerField())
    errors          = serializers.ListField(child=serializers.DictField())
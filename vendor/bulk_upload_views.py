"""
product/bulk_upload_views.py

API endpoints for the bulk product upload feature.

Endpoints:
  GET  /api/v1/vendor/products/bulk-upload/template/  → Download CSV template
  POST /api/v1/vendor/products/bulk-upload/           → Upload & process file
  GET  /api/v1/vendor/products/bulk-upload/meta/      → Sub-categories, brands for UI dropdowns

Access control:
  - Vendor must be authenticated, approved, and have can_access_bulk_upload=True on their plan.
    (Enforced via the SubscriptionGateMixin / require_feature permission factory.)
  - Uses the existing payments.subscription_permissions module.
"""

import csv
import io
import logging
from django.http import HttpResponse
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from payments.subscription_permissions import require_feature
from product.models import Sub_Category, Brand, Product
from vendor.bulk_upload_serializer import (
    BulkUploadResultSerializer,
    BulkProductRowSerializer,
    REQUIRED_COLUMNS,
)

logger = logging.getLogger(__name__)

# ── Template column definitions ────────────────────────────────────────────────
TEMPLATE_HEADERS = [
    "title",
    "price",
    "old_price",
    "sub_category_slug",
    "brand_slug",
    "product_type",
    "total_quantity",
    "weight",
    "volume",
    "life",
    "variant",
    "description",
    "features",
    "specifications",
    "delivery_returns",
    "size_names",
    "color_names",
    "color_codes",
    "variant_prices",
    "variant_quantities",
]

EXAMPLE_ROW = [
    "Premium Cotton T-Shirt",       # title
    "49.99",                        # price
    "69.99",                        # old_price
    "mens-clothing",                # sub_category_slug  (use exact slug)
    "nike",                         # brand_slug          (leave blank if none)
    "new",                          # product_type: new/used/book/grocery/refurbished
    "200",                          # total_quantity
    "0.3",                          # weight (kg)
    "0.001",                        # volume (m³)
    "2 years",                      # life
    "Size-Color",                   # variant: None/Size/Color/Size-Color
    "High quality cotton tee",      # description
    "100% organic cotton",          # features
    "Machine wash cold",            # specifications
    "Free returns within 30 days",  # delivery_returns
    "S;M;L;XL",                     # size_names (semicolon-separated)
    "Red;Blue;Black",               # color_names (semicolon-separated)
    "#FF0000;#0000FF;#000000",      # color_codes (semicolon-separated)
    "49.99;49.99;49.99",            # variant_prices (one per size×color combo, row-major)
    "20;20;20",                     # variant_quantities
]


def _get_vendor(user):
    """Return the Vendor linked to this user, or None."""
    return getattr(user, "vendor_user", None)


class BulkUploadTemplatAPIView(APIView):
    """
    GET /api/v1/vendor/products/bulk-upload/template/
    Returns a downloadable CSV template with headers + one example row.
    """
    permission_classes = [IsAuthenticated, require_feature("can_access_bulk_upload")]

    def get(self, request):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(TEMPLATE_HEADERS)
        writer.writerow(EXAMPLE_ROW)

        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="negromart_bulk_upload_template.csv"'
        return response


class BulkUploadMetaAPIView(APIView):
    """
    GET /api/v1/vendor/products/bulk-upload/meta/
    Returns sub-categories and brands so the frontend can render helper dropdowns.
    """
    permission_classes = [IsAuthenticated, require_feature("can_access_bulk_upload")]

    def get(self, request):
        sub_cats = Sub_Category.objects.select_related("category__main_category").values(
            "id", "title", "slug",
        )
        brands = Brand.objects.values("id", "title", "slug")

        return Response({
            "sub_categories": list(sub_cats),
            "brands": list(brands),
            "product_types": ["new", "used", "book", "grocery", "refurbished"],
            "variant_types": ["None", "Size", "Color", "Size-Color"],
            "required_columns": REQUIRED_COLUMNS,
            "template_headers": TEMPLATE_HEADERS,
        })


class BulkProductUploadAPIView(APIView):
    """
    POST /api/v1/vendor/products/bulk-upload/
    Accepts multipart/form-data with a 'file' field (CSV or TSV).

    Returns:
        200  { total_rows, success_count, failed_count, created_product_ids, errors }
    """
    permission_classes = [IsAuthenticated, require_feature("can_access_bulk_upload")]

    # Protect server from huge files
    MAX_ROWS = 500

    def post(self, request):
        vendor = _get_vendor(request.user)
        if not vendor:
            return Response(
                {"error": "No vendor account associated with this user."},
                status=status.HTTP_403_FORBIDDEN,
            )

        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return Response(
                {"error": "No file uploaded. Send a CSV file with field name 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Detect delimiter ────────────────────────────────────────────────
        filename = uploaded_file.name.lower()
        delimiter = "\t" if filename.endswith(".tsv") else ","

        # ── Decode ─────────────────────────────────────────────────────────
        try:
            content = uploaded_file.read().decode("utf-8-sig")  # handle BOM
        except UnicodeDecodeError:
            return Response(
                {"error": "File encoding not supported. Please use UTF-8."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

        # ── Validate headers ────────────────────────────────────────────────
        if not reader.fieldnames:
            return Response(
                {"error": "File appears to be empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames)
        if missing:
            return Response(
                {
                    "error": f"Missing required columns: {', '.join(sorted(missing))}",
                    "found_columns": reader.fieldnames,
                    "required_columns": REQUIRED_COLUMNS,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Process rows ────────────────────────────────────────────────────
        rows = list(reader)
        if len(rows) > self.MAX_ROWS:
            return Response(
                {"error": f"File exceeds maximum of {self.MAX_ROWS} rows per upload."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created_ids = []
        errors = []

        for i, raw_row in enumerate(rows, start=2):  # row 1 = headers
            # Clean whitespace from every cell
            row = {k.strip(): (v.strip() if v else "") for k, v in raw_row.items() if k}

            # Skip completely blank rows
            if not any(row.values()):
                continue

            serializer = BulkProductRowSerializer(data=row)

            if not serializer.is_valid():
                errors.append({
                    "row": i,
                    "title": row.get("title", "—"),
                    "errors": serializer.errors,
                })
                continue

            try:
                with transaction.atomic():
                    product = serializer.save(vendor=vendor)
                    created_ids.append(product.id)
            except Exception as exc:
                logger.error(
                    f"BulkUpload: row {i} failed to save for vendor {vendor.id}: {exc}",
                    exc_info=True,
                )
                errors.append({
                    "row": i,
                    "title": row.get("title", "—"),
                    "errors": {"non_field_errors": [str(exc)]},
                })

        result = {
            "total_rows":   len(rows),
            "success_count": len(created_ids),
            "failed_count":  len(errors),
            "created_product_ids": created_ids,
            "errors": errors,
        }

        out = BulkUploadResultSerializer(result)
        http_status = (
            status.HTTP_207_MULTI_STATUS if errors and created_ids
            else status.HTTP_400_BAD_REQUEST if not created_ids
            else status.HTTP_201_CREATED
        )
        return Response(out.data, status=http_status)
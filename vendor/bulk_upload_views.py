"""
vendor/bulk_upload_views.py

Bulk product upload endpoints.

  GET  /api/v1/vendor/products/bulk-upload/template/       → CSV template download
  GET  /api/v1/vendor/products/bulk-upload/meta/           → Dropdowns + plan info
  POST /api/v1/vendor/products/bulk-upload/                → CSV / TSV file upload
  POST /api/v1/vendor/products/bulk-upload/direct/         → JSON rows (in-browser grid)
  GET  /api/v1/vendor/products/bulk-upload/job/<uuid>/     → Async job status

Access: IsVerifiedVendor + require_feature("can_access_bulk_upload")
Routing: ≤ SYNC_THRESHOLD rows → synchronous; > SYNC_THRESHOLD rows → Celery task.
"""

import csv
import io
import logging

from django.db.models import F
from django.http import HttpResponse
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from payments.models import SubscriptionUsage
from payments.subscription_permissions import require_feature, _usage, _plan
from product.models import Sub_Category, Brand
from vendor.permissions import IsVerifiedVendor
from vendor.bulk_upload_serializer import (
    BulkUploadResultSerializer,
    BulkProductRowSerializer,
    REQUIRED_COLUMNS,
)

logger = logging.getLogger(__name__)

SYNC_THRESHOLD = 100  # rows ≤ this processed synchronously

# ── CSV template columns ───────────────────────────────────────────────────────
TEMPLATE_HEADERS = [
    "title", "price", "old_price", "sub_category_slug", "brand_slug",
    "product_type", "total_quantity", "weight", "volume", "life",
    "variant", "description", "features", "specifications",
    "delivery_returns", "size_names", "color_names", "color_codes",
    "variant_prices", "variant_quantities",
]

EXAMPLE_ROW = [
    "Premium Cotton T-Shirt", "49.99", "69.99", "mens-clothing", "nike",
    "new", "200", "0.3", "0.001", "2 years",
    "Size-Color", "High quality cotton tee", "100% organic cotton",
    "Machine wash cold", "Free returns within 30 days",
    "S;M;L;XL", "Red;Blue;Black", "#FF0000;#0000FF;#000000",
    "49.99;49.99;49.99", "20;20;20",
]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_vendor(user):
    return getattr(user, "vendor_user", None)


def _check_product_limit(user, requested_count):
    usage = _usage(user)
    plan  = _plan(user)
    if not plan or not plan.max_products:
        return usage, None
    current   = usage.active_products_count if usage else 0
    remaining = max(plan.max_products - current, 0)
    if current + requested_count > plan.max_products:
        return None, {
            "error":           "product_limit_exceeded",
            "detail":          (
                f"This would exceed your plan limit of {plan.max_products} products. "
                f"You have {remaining} slot{'s' if remaining != 1 else ''} remaining."
            ),
            "limit":           plan.max_products,
            "current_count":   current,
            "remaining_slots": remaining,
            "requested":       requested_count,
            "action":          "upgrade",
            "upgrade_url":     "/subscribe",
        }
    return usage, None


def _run_rows_sync(vendor, rows, usage):
    """Process rows synchronously. Returns a DRF Response."""
    created_ids, errors = [], []

    for i, row in enumerate(rows, start=2):
        ser = BulkProductRowSerializer(data=row)
        if not ser.is_valid():
            errors.append({"row": i, "title": row.get("title", "—"), "errors": ser.errors})
            continue
        try:
            with transaction.atomic():
                product = ser.save(vendor=vendor)
                created_ids.append(product.id)
        except Exception as exc:
            logger.error("BulkUpload sync row %d vendor %d: %s", i, vendor.id, exc, exc_info=True)
            errors.append({"row": i, "title": row.get("title", "—"), "errors": {"non_field_errors": [str(exc)]}})

    if created_ids and usage:
        SubscriptionUsage.objects.filter(pk=usage.pk).update(
            active_products_count=F("active_products_count") + len(created_ids)
        )

    result = {
        "mode": "sync",
        "total_rows":          len(rows),
        "success_count":       len(created_ids),
        "failed_count":        len(errors),
        "created_product_ids": created_ids,
        "errors":              errors,
    }
    http_status = (
        status.HTTP_207_MULTI_STATUS if errors and created_ids
        else status.HTTP_400_BAD_REQUEST if not created_ids
        else status.HTTP_201_CREATED
    )
    return Response(BulkUploadResultSerializer(result).data, status=http_status)


def _run_rows_async(vendor, rows):
    """Dispatch rows to Celery. Returns a DRF Response with job metadata."""
    from vendor.models import BulkUploadJob
    from vendor.tasks import process_bulk_upload

    job = BulkUploadJob.objects.create(vendor=vendor, total_rows=len(rows))
    process_bulk_upload.delay(str(job.id), rows, vendor.id)
    return Response(
        {
            "mode":       "async",
            "job_id":     str(job.id),
            "total_rows": len(rows),
            "status":     job.status,
            "status_url": f"/api/v1/vendor/products/bulk-upload/job/{job.id}/",
        },
        status=status.HTTP_202_ACCEPTED,
    )


def _dispatch(vendor, clean_rows, usage):
    if len(clean_rows) <= SYNC_THRESHOLD:
        return _run_rows_sync(vendor, clean_rows, usage)
    return _run_rows_async(vendor, clean_rows)


# ── Template download ──────────────────────────────────────────────────────────

class BulkUploadTemplatAPIView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor, require_feature("can_access_bulk_upload")]

    def get(self, request):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(TEMPLATE_HEADERS)
        writer.writerow(EXAMPLE_ROW)
        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="negromart_bulk_upload_template.csv"'
        return response


# ── Metadata for dropdowns ─────────────────────────────────────────────────────

class BulkUploadMetaAPIView(APIView):
    """
    Returns all data the frontend needs to render its dropdowns and reference panels.
    sub_categories includes a 'label' field with the full display path
    (Main Category > Category > Sub Category) so the frontend doesn't need slugs.
    """
    permission_classes = [IsAuthenticated, IsVerifiedVendor, require_feature("can_access_bulk_upload")]

    def get(self, request):
        sub_cats_qs = Sub_Category.objects.select_related(
            "category__main_category"
        ).order_by("category__main_category__title", "category__title", "title")

        sub_categories = []
        for sc in sub_cats_qs:
            cat      = sc.category
            main_cat = cat.main_category
            sub_categories.append({
                "id":    sc.id,
                "slug":  sc.slug,
                "title": sc.title,
                "label": f"{main_cat.title} › {cat.title} › {sc.title}",
            })

        brands = list(Brand.objects.values("id", "title", "slug").order_by("title"))

        usage = _usage(request.user)
        plan  = _plan(request.user)
        current         = usage.active_products_count if usage else 0
        remaining_slots = (
            max(plan.max_products - current, 0) if plan and plan.max_products else None
        )

        return Response({
            "sub_categories":  sub_categories,
            "brands":          brands,
            "product_types":   [
                {"value": "new",         "label": "New"},
                {"value": "used",        "label": "Used / Second-hand"},
                {"value": "book",        "label": "Book"},
                {"value": "grocery",     "label": "Grocery / Food"},
                {"value": "refurbished", "label": "Refurbished"},
            ],
            "variant_types":   [
                {"value": "None",       "label": "No variants"},
                {"value": "Size",       "label": "By size  (S, M, L …)"},
                {"value": "Color",      "label": "By color"},
                {"value": "Size-Color", "label": "By size AND color"},
            ],
            "required_columns":  REQUIRED_COLUMNS,
            "template_headers":  TEMPLATE_HEADERS,
            "sync_threshold":    SYNC_THRESHOLD,
            "max_rows":          500,
            "remaining_slots":   remaining_slots,
            "plan_name":         plan.name if plan else None,
        })


# ── CSV / TSV file upload ──────────────────────────────────────────────────────

class BulkProductUploadAPIView(APIView):
    """POST multipart/form-data with field 'file' (CSV or TSV)."""
    permission_classes = [IsAuthenticated, IsVerifiedVendor, require_feature("can_access_bulk_upload")]
    MAX_ROWS = 500

    def post(self, request):
        vendor = _get_vendor(request.user)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return Response({"error": "No file uploaded."}, status=status.HTTP_400_BAD_REQUEST)

        filename  = uploaded_file.name.lower()
        delimiter = "\t" if filename.endswith(".tsv") else ","
        try:
            content = uploaded_file.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return Response({"error": "File encoding not supported. Use UTF-8."}, status=status.HTTP_400_BAD_REQUEST)

        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        if not reader.fieldnames:
            return Response({"error": "File appears to be empty."}, status=status.HTTP_400_BAD_REQUEST)

        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames)
        if missing:
            return Response({
                "error":            f"Missing required columns: {', '.join(sorted(missing))}",
                "found_columns":    list(reader.fieldnames),
                "required_columns": REQUIRED_COLUMNS,
            }, status=status.HTTP_400_BAD_REQUEST)

        raw_rows = list(reader)
        if len(raw_rows) > self.MAX_ROWS:
            return Response({"error": f"File exceeds the {self.MAX_ROWS}-row limit."}, status=status.HTTP_400_BAD_REQUEST)

        clean_rows = [
            {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
            for row in raw_rows
            if any(v.strip() for v in row.values() if v)
        ]
        if not clean_rows:
            return Response({"error": "File has no data rows."}, status=status.HTTP_400_BAD_REQUEST)

        usage, limit_error = _check_product_limit(request.user, len(clean_rows))
        if limit_error:
            return Response(limit_error, status=status.HTTP_403_FORBIDDEN)

        return _dispatch(vendor, clean_rows, usage)


# ── JSON / in-browser grid upload ──────────────────────────────────────────────

class BulkProductDirectAPIView(APIView):
    """
    POST /api/v1/vendor/products/bulk-upload/direct/
    Body: { "products": [ { field: value, … }, … ] }

    Accepts the same row schema as the CSV upload.
    Used by the in-browser grid editor so sellers never touch a spreadsheet.
    """
    permission_classes = [IsAuthenticated, IsVerifiedVendor, require_feature("can_access_bulk_upload")]
    MAX_ROWS = 500

    def post(self, request):
        vendor = _get_vendor(request.user)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        products = request.data.get("products")
        if not isinstance(products, list) or not products:
            return Response({"error": "Provide a non-empty 'products' list."}, status=status.HTTP_400_BAD_REQUEST)

        if len(products) > self.MAX_ROWS:
            return Response({"error": f"Maximum {self.MAX_ROWS} products per upload."}, status=status.HTTP_400_BAD_REQUEST)

        # Drop completely empty rows (grid may send trailing blank rows)
        clean_rows = [
            {str(k): str(v).strip() if v is not None else "" for k, v in row.items()}
            for row in products
            if isinstance(row, dict) and any(str(v).strip() for v in row.values() if v)
        ]
        if not clean_rows:
            return Response({"error": "No products provided."}, status=status.HTTP_400_BAD_REQUEST)

        usage, limit_error = _check_product_limit(request.user, len(clean_rows))
        if limit_error:
            return Response(limit_error, status=status.HTTP_403_FORBIDDEN)

        return _dispatch(vendor, clean_rows, usage)


# ── Async job status ───────────────────────────────────────────────────────────

class BulkUploadJobStatusAPIView(APIView):
    """GET  /api/v1/vendor/products/bulk-upload/job/<uuid>/"""
    permission_classes = [IsAuthenticated, IsVerifiedVendor, require_feature("can_access_bulk_upload")]

    def get(self, request, job_id):
        vendor = _get_vendor(request.user)
        if not vendor:
            return Response({"error": "No vendor account."}, status=status.HTTP_403_FORBIDDEN)

        from vendor.models import BulkUploadJob
        try:
            job = BulkUploadJob.objects.get(id=job_id, vendor=vendor)
        except BulkUploadJob.DoesNotExist:
            return Response({"error": "Job not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "job_id":              str(job.id),
            "status":              job.status,
            "total_rows":          job.total_rows,
            "success_count":       job.success_count,
            "failed_count":        job.failed_count,
            "created_product_ids": job.created_product_ids,
            "errors":              job.errors,
            "error_message":       job.error_message,
            "created_at":          job.created_at,
            "updated_at":          job.updated_at,
        })

"""
vendor/bulk_upload_views.py

Bulk product upload endpoints (the format and the work: vendor/bulk_upload.py).

  GET  /api/v1/vendor/products/bulk-upload/template/   → CSV template with example rows
  GET  /api/v1/vendor/products/bulk-upload/meta/       → columns, categories, brands, countries,
                                                          delivery methods, store defaults, plan
  POST /api/v1/vendor/products/bulk-upload/            → CSV/TSV file (field "file")
  POST /api/v1/vendor/products/bulk-upload/direct/     → the in-browser table: "products" JSON
                                                          (+ photo files, multipart)
  GET  /api/v1/vendor/products/bulk-upload/job/<uuid>/ → background job status

Access: verified store, plan with can_access_bulk_upload, catalogue permission.
Small uploads run in the request. Large ones, and any that must download
photos from links, run as a background job the page polls.
"""

import csv
import io
import json
import logging

from django.http import HttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from payments.entitlements import plan_for_vendor, product_count
from payments.subscription_permissions import require_feature
from product import variations
from product.models import Brand, DeliveryOption, Sub_Category
from vendor import bulk_upload
from vendor.access import Capability, require_capability
from vendor.permissions import IsVerifiedVendor

logger = logging.getLogger(__name__)

SYNC_THRESHOLD = 100            # products handled in the request; more go to a background job
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TABLE_PHOTOS = 400          # files in one table upload

PERMISSIONS = [IsAuthenticated, IsVerifiedVendor, require_feature("can_access_bulk_upload"),
               require_capability(Capability.MANAGE_CATALOG)]


def _vendor(request):
    return getattr(request.user, "current_vendor", None)


def _limit_error(vendor, requested):
    """Early, friendly refusal; each product is still checked under a lock when created."""
    plan = plan_for_vendor(vendor)
    if not plan or not plan.max_products:
        return None
    current = product_count(vendor)
    remaining = max(plan.max_products - current, 0)
    if current + requested <= plan.max_products:
        return None
    return Response({
        "error": "product_limit_exceeded",
        "detail": (f"This would go over your plan's {plan.max_products} products. "
                   f"You have room for {remaining} more."),
        "limit": plan.max_products, "current_count": current, "remaining_slots": remaining,
        "requested": requested, "action": "upgrade", "upgrade_url": "/subscribe",
    }, status=status.HTTP_403_FORBIDDEN)


def _run(vendor, specs, files_by_spec=None):
    if not specs:
        return Response({"error": "There are no products to upload (example rows starting with # are skipped)."},
                        status=status.HTTP_400_BAD_REQUEST)
    limit = _limit_error(vendor, len(specs))
    if limit:
        return limit
    background = not files_by_spec and (len(specs) > SYNC_THRESHOLD or bulk_upload.specs_need_downloads(specs))
    if background:
        from vendor.models import BulkUploadJob
        from vendor.tasks import process_bulk_upload
        job = BulkUploadJob.objects.create(vendor=vendor, total_rows=len(specs))
        process_bulk_upload.delay(str(job.id), specs, vendor.id)
        return Response({
            "mode": "async", "job_id": str(job.id), "total_rows": len(specs), "status": job.status,
            "status_url": f"/api/v1/vendor/products/bulk-upload/job/{job.id}/",
        }, status=status.HTTP_202_ACCEPTED)

    report = bulk_upload.process_specs(vendor, specs, files_by_spec)
    code = (status.HTTP_201_CREATED if not report["errors"]
            else status.HTTP_207_MULTI_STATUS if report["products"] else status.HTTP_400_BAD_REQUEST)
    return Response({"mode": "sync", **report}, status=code)


def _decode(raw):
    # Excel on Windows saves CSV in the local code page, not UTF-8.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


# ── Template ─────────────────────────────────────────────────────────────────

class BulkUploadTemplatAPIView(APIView):
    permission_classes = PERMISSIONS

    def get(self, request):
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=bulk_upload.TEMPLATE_HEADERS)
        writer.writeheader()
        writer.writerows(bulk_upload.template_rows())
        # BOM: Excel then opens the file as UTF-8 (₵, accents).
        response = HttpResponse("﻿" + output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="negromart_bulk_upload_template.csv"'
        return response


# ── What the page needs ──────────────────────────────────────────────────────

class BulkUploadMetaAPIView(APIView):
    permission_classes = PERMISSIONS

    def get(self, request):
        from address.models import Country

        vendor = _vendor(request)
        sub_categories = [{
            "id": sc.id, "slug": sc.slug, "title": sc.title,
            "label": " › ".join(t for t in (getattr(getattr(sc.category, "main_category", None), "title", None),
                                            getattr(sc.category, "title", None), sc.title) if t),
        } for sc in Sub_Category.objects.select_related("category__main_category")
            .order_by("category__main_category__title", "category__title", "title")]

        ctx = bulk_upload.Context(vendor) if vendor else None
        plan = plan_for_vendor(vendor) if vendor else None
        remaining = None
        if plan and plan.max_products:
            remaining = max(plan.max_products - product_count(vendor), 0)

        return Response({
            "columns": [{"name": n, "required": r, "help": h, "example": e} for n, r, h, e in bulk_upload.COLUMNS],
            "required_columns": bulk_upload.REQUIRED_COLUMNS,
            "template_headers": bulk_upload.TEMPLATE_HEADERS,
            "sub_categories": sub_categories,
            "brands": list(Brand.objects.values("id", "title", "slug").order_by("title")),
            "countries": list(Country.objects.values("id", "name", "code").order_by("name")),
            "delivery_methods": list(DeliveryOption.objects.values("id", "name", "type", "min_days", "max_days")
                                     .order_by("type", "min_days", "id")),
            "conditions": [
                {"value": "new", "label": "New"}, {"value": "used", "label": "Used"},
                {"value": "refurbished", "label": "Refurbished"}, {"value": "grocery", "label": "Grocery / food"},
                {"value": "book", "label": "Book"},
            ],
            "defaults": {
                "ships_to": [{"id": c.id, "name": c.name, "code": c.code} for c in (ctx.default_regions if ctx else [])],
                "delivery_methods": [{"id": d.id, "name": d.name} for d in (ctx.default_delivery if ctx else [])],
            },
            "variation_options": {
                "types": variations.OPTION_TYPES, "suggested": variations.SUGGESTED,
                "default_suggested": variations.DEFAULT_SUGGESTED, "max_axes": variations.MAX_AXES,
            },
            "max_images_per_product": plan.max_images_per_product if plan else None,
            "max_images_per_variant": plan.max_images_per_variant if plan else 0,
            "sync_threshold": SYNC_THRESHOLD,
            "max_rows": bulk_upload.MAX_ROWS,
            "remaining_slots": remaining,
            "plan_name": plan.name if plan else None,
        })


# ── Spreadsheet file ─────────────────────────────────────────────────────────

class BulkProductUploadAPIView(APIView):
    """POST multipart/form-data with field 'file' (.csv or .tsv)."""
    permission_classes = PERMISSIONS

    def post(self, request):
        vendor = _vendor(request)
        if not vendor:
            return Response({"error": "No store account."}, status=status.HTTP_403_FORBIDDEN)
        upload = request.FILES.get("file")
        if not upload:
            return Response({"error": "Choose a file to upload."}, status=status.HTTP_400_BAD_REQUEST)
        name = upload.name.lower()
        if name.endswith((".xlsx", ".xls", ".numbers", ".ods")):
            return Response({"error": "Save the sheet as CSV first: in Excel, File › Save As › CSV UTF-8; "
                                      "in Google Sheets, File › Download › Comma-separated values."},
                            status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_FILE_BYTES:
            return Response({"error": "The file is larger than 5 MB."}, status=status.HTTP_400_BAD_REQUEST)
        content = _decode(upload.read())
        if content is None:
            return Response({"error": "The file could not be read. Save it as CSV UTF-8 and try again."},
                            status=status.HTTP_400_BAD_REQUEST)

        delimiter = "\t" if name.endswith(".tsv") else ","
        if delimiter == "," and content[:2048].count(";") > content[:2048].count(","):
            delimiter = ";"     # Excel in many locales separates with semicolons
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        if not reader.fieldnames:
            return Response({"error": "The file is empty."}, status=status.HTTP_400_BAD_REQUEST)
        found = {bulk_upload.ALIASES.get(h.strip().lower().replace(" ", "_"), h.strip().lower().replace(" ", "_"))
                 for h in reader.fieldnames if h}
        missing = [c for c in bulk_upload.REQUIRED_COLUMNS if c not in found]
        if missing:
            return Response({
                "error": f"Missing columns: {', '.join(missing)}. Start from the template.",
                "found_columns": list(reader.fieldnames), "required_columns": bulk_upload.REQUIRED_COLUMNS,
            }, status=status.HTTP_400_BAD_REQUEST)

        rows = list(reader)
        if len(rows) > bulk_upload.MAX_ROWS:
            return Response({"error": f"Up to {bulk_upload.MAX_ROWS} rows per file. Split it into smaller files."},
                            status=status.HTTP_400_BAD_REQUEST)
        return _run(vendor, bulk_upload.rows_to_specs(rows))


# ── In-browser table ─────────────────────────────────────────────────────────

class BulkProductDirectAPIView(APIView):
    """
    POST "products": a JSON list, as JSON or a multipart field. Each item is
    either one flat row (the CSV columns) or {"rows": [row, ...]} for a product
    with versions (one row per version). Photos attached in the table come as
    files "photos_<product index>" (several) and "version_photo_<product>_<version>".
    """
    permission_classes = PERMISSIONS

    def post(self, request):
        vendor = _vendor(request)
        if not vendor:
            return Response({"error": "No store account."}, status=status.HTTP_403_FORBIDDEN)
        products = request.data.get("products")
        if isinstance(products, str):
            try:
                products = json.loads(products)
            except json.JSONDecodeError:
                return Response({"error": "The products could not be read."}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(products, list) or not products:
            return Response({"error": "Add at least one product."}, status=status.HTTP_400_BAD_REQUEST)
        if len(products) > bulk_upload.MAX_ROWS:
            return Response({"error": f"Up to {bulk_upload.MAX_ROWS} products at once."},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(request.FILES) > MAX_TABLE_PHOTOS:
            return Response({"error": f"Up to {MAX_TABLE_PHOTOS} photos per upload."},
                            status=status.HTTP_400_BAD_REQUEST)

        specs, files_by_spec = [], {}
        for index, item in enumerate(products):
            rows = item.get("rows") if isinstance(item, dict) and isinstance(item.get("rows"), list) else [item]
            rows = [bulk_upload.normalise_row(r) for r in rows if isinstance(r, dict)]
            rows = [r for r in rows if any(r.values())]
            if not rows:
                continue
            spec_index = len(specs)
            specs.append({"rows": [index + 1], "product": rows[0], "versions": rows})
            photos = request.FILES.getlist(f"photos_{index}")
            version_photos = {}
            for v in range(len(rows)):
                # Several files: the first is the version's photo, the rest its gallery.
                uploads = request.FILES.getlist(f"version_photo_{index}_{v}")
                if uploads:
                    version_photos[v] = uploads
            if photos or version_photos:
                files_by_spec[spec_index] = {"images": photos, "version_images": version_photos}
        return _run(vendor, specs, files_by_spec)


# ── Background job ───────────────────────────────────────────────────────────

class BulkUploadJobStatusAPIView(APIView):
    permission_classes = PERMISSIONS

    def get(self, request, job_id):
        vendor = _vendor(request)
        if not vendor:
            return Response({"error": "No store account."}, status=status.HTTP_403_FORBIDDEN)
        from vendor.models import BulkUploadJob
        try:
            job = BulkUploadJob.objects.get(id=job_id, vendor=vendor)
        except BulkUploadJob.DoesNotExist:
            return Response({"error": "Upload not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "job_id": str(job.id), "status": job.status, "total_rows": job.total_rows,
            "success_count": job.success_count, "failed_count": job.failed_count,
            "draft_count": sum(1 for p in job.results if p.get("status") == "draft"),
            "created_product_ids": job.created_product_ids, "products": job.results,
            "errors": job.errors, "error_message": job.error_message,
            "created_at": job.created_at, "updated_at": job.updated_at,
        })

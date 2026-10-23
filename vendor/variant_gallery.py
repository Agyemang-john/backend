"""
vendor/variant_gallery.py
Extra gallery photos for a product variant (product.VariantImage).

The variant's main photo still travels with the product form. Extra photos are
uploaded one per request to POST vendor/variants/<id>/images/ (optional
`position`) after the product is saved, so a product with many variants never becomes one huge request
(nginx caps a request at 50 MB). Removing and reordering are part of the normal
product save (`keep_gallery` on each variant, vendor/product_serializers.py).

Paid feature: plan.max_images_per_variant photos per variant, 0 on Free.
Like listing limits, the limit only blocks adding: after a downgrade the photos
a variant already has stay up and can still be removed.

Photos are re-encoded to JPEG (EXIF dropped, longest side capped) before they
go to Spaces, which keeps storage small whatever the seller uploads.
"""

import uuid

from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from payments.entitlements import plan_for_vendor, variant_image_limit
from product.models import VariantImage, Variants
from product.review_media import _IMAGE_FORMATS, _jpeg
from vendor.access import Capability, require_capability
from vendor.permissions import IsVerifiedVendor

from .product_serializers import VariantImageSerializer

MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # the dashboard sends ≤ 2 MB; allow some slack
MAX_SIDE = 1800                      # same ceiling the dashboard validates against
JPEG_QUALITY = 85


def _encode(upload):
    if upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError({'image': "Photos can be up to 5 MB."})
    try:
        with Image.open(upload) as original:
            if original.format not in _IMAGE_FORMATS:
                raise ValidationError({'image': "Upload a photo as JPG, PNG or WebP."})
            original.load()
            img = ImageOps.exif_transpose(original)
    except (UnidentifiedImageError, OSError):
        raise ValidationError({'image': "This photo could not be read."})
    _, content = _jpeg(img, MAX_SIDE, JPEG_QUALITY)
    content.name = f"{uuid.uuid4().hex}.jpg"
    return content


def _position(raw):
    """The slot the seller placed the photo in (see keep_gallery), else None = append."""
    try:
        return min(max(int(raw), 0), 32767)
    except (TypeError, ValueError):
        return None


def _limit_error(vendor, limit):
    plan = plan_for_vendor(vendor)
    tier = plan.tier if plan else 'free'
    if limit == 0:
        return {
            'error': 'plan_upgrade_required',
            'detail': f"Your current plan ({tier}) does not include extra variant photos. "
                      "Upgrade to add more photos to each variant.",
            'current_tier': tier,
            'required_tier': 'basic',
            'action': 'upgrade',
            'upgrade_url': '/subscribe',
        }
    return {
        'error': 'variant_image_limit_reached',
        'detail': f"Your plan allows {limit} extra photos per variant. Upgrade to add more.",
        'limit': limit,
        'current_tier': tier,
        'action': 'upgrade',
        'upgrade_url': '/subscribe',
    }


class VariantGalleryUploadView(APIView):
    permission_classes = [IsAuthenticated, IsVerifiedVendor, require_capability(Capability.MANAGE_CATALOG)]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, variant_id):
        vendor = request.user.current_vendor
        upload = request.FILES.get('image')
        if not upload:
            raise ValidationError({'image': "Choose a photo to upload."})

        limit = variant_image_limit(vendor)
        if limit == 0:
            raise PermissionDenied(_limit_error(vendor, limit))
        content = _encode(upload)

        with transaction.atomic():
            # Row lock: parallel uploads to one variant can't overshoot the limit.
            variant = get_object_or_404(
                Variants.objects.select_for_update(), id=variant_id, product__vendor=vendor,
            )
            gallery = variant.variantimage_set.all()
            if gallery.count() >= limit:
                raise PermissionDenied(_limit_error(vendor, limit))
            position = _position(request.data.get('position'))
            if position is None:
                last = gallery.aggregate(m=Max('position'))['m']
                position = 0 if last is None else last + 1
            image = VariantImage.objects.create(variant=variant, images=content, position=position)

        data = VariantImageSerializer(image, context={'request': request}).data
        return Response(data, status=status.HTTP_201_CREATED)

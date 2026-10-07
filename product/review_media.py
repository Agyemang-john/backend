"""
product/review_media.py
Validation and processing for photos/videos attached to reviews.

Images
  - accepted: JPEG, PNG, WebP (HEIC is refused: most browsers can't display it)
  - re-encoded to JPEG: removes EXIF metadata (phones embed GPS location),
    fixes orientation, caps the longest side at IMAGE_MAX_SIDE
  - a THUMB_SIDE thumbnail is stored for grids and the gallery strip

Videos
  - accepted: MP4, WebM, QuickTime (.mov); identified by file signature, not
    by the name or the browser-declared type
  - stored as uploaded; browsers render a poster frame with preload="metadata"

Limits are module constants so they're easy to find and change. Uploads go
through Django today; if video volume grows, switch to presigned uploads
straight to Spaces and run these checks when the client registers the upload.
"""

from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_MAX_BYTES = 10 * 1024 * 1024        # 10 MB as uploaded
VIDEO_MAX_BYTES = 30 * 1024 * 1024        # 30 MB (nginx allows 50 MB)
IMAGE_MAX_SIDE = 1600
THUMB_SIDE = 400
MAX_IMAGES_PER_REVIEW = 6
MAX_VIDEOS_PER_REVIEW = 1

_IMAGE_FORMATS = {'JPEG', 'PNG', 'WEBP', 'MPO'}  # MPO = multi-picture JPEG from some phones


class MediaError(ValueError):
    """Message is safe to show to the shopper."""


def _video_kind(head: bytes):
    """Identify MP4/MOV (ISO base media 'ftyp' box) or WebM (EBML header)."""
    if len(head) >= 12 and head[4:8] == b'ftyp':
        return 'video/quicktime' if head[8:10] == b'qt' else 'video/mp4'
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return 'video/webm'
    return None


def detect_kind(upload):
    """Return 'image' or 'video' for an uploaded file, or raise MediaError."""
    head = upload.read(16)
    upload.seek(0)
    if _video_kind(head):
        return 'video'
    try:
        with Image.open(upload) as img:
            fmt = img.format
        upload.seek(0)
    except (UnidentifiedImageError, OSError):
        raise MediaError("Upload a photo (JPG, PNG or WebP) or a video (MP4, WebM or MOV).")
    if fmt not in _IMAGE_FORMATS:
        raise MediaError("Upload a photo as JPG, PNG or WebP.")
    return 'image'


def _jpeg(img, max_side, quality):
    img = img.copy()
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    if img.mode not in ('RGB', 'L'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        background.paste(img, mask=img.convert('RGBA').split()[-1])
        img = background
    buffer = BytesIO()
    img.convert('RGB').save(buffer, format='JPEG', quality=quality, optimize=True, progressive=True)
    return img.size, ContentFile(buffer.getvalue())


def process_image(upload):
    """Return (full ContentFile, thumbnail ContentFile, (width, height))."""
    if upload.size > IMAGE_MAX_BYTES:
        raise MediaError("Photos can be up to 10 MB.")
    try:
        with Image.open(upload) as original:
            original.load()
            img = ImageOps.exif_transpose(original)  # apply rotation, then drop EXIF entirely
    except (UnidentifiedImageError, OSError):
        raise MediaError("This photo could not be read.")
    (width, height), full = _jpeg(img, IMAGE_MAX_SIDE, 85)
    _, thumb = _jpeg(img, THUMB_SIDE, 78)
    return full, thumb, (width, height)


def check_video(upload):
    if upload.size > VIDEO_MAX_BYTES:
        raise MediaError("Videos can be up to 40 MB. Try a shorter clip.")
    head = upload.read(16)
    upload.seek(0)
    if not _video_kind(head):
        raise MediaError("Upload a video as MP4, WebM or MOV.")

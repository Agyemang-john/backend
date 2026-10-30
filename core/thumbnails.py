"""
core/thumbnails.py
Small WebP copies of uploaded images, so product cards don't download the
full photo (up to ~900 KB) for a ~180 px slot.

The storefront serves images straight from the Spaces CDN (Vercel's image
optimizer is off), and its image loader (frontend lib/imageLoader.ts) asks for

    <media>/thumbs/<width>/<original path without extension>.webp

for every jpg/png/webp under media/. So a copy must exist at every width:
- MediaStorage makes them when a file is uploaded;
- `manage.py make_thumbnails` makes them for files uploaded before this.
Images narrower than a width are copied at their own size (never upscaled),
so the URL the loader builds always exists.
"""

import io
import logging
import os

from django.core.files.base import ContentFile
from PIL import Image, ImageOps
from storages.backends.s3boto3 import S3Boto3Storage

logger = logging.getLogger(__name__)

# Keep in sync with WIDTHS in frontend lib/imageLoader.ts.
WIDTHS = (256, 384, 640, 1080)
PREFIX = 'thumbs/'
EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
QUALITY = 78

# Uploaded names are unique (AWS_S3_FILE_OVERWRITE = False) and never change in
# place, so browsers and the CDN may keep them for a year.
LONG_CACHE = 'public, max-age=31536000, immutable'


def thumb_name(name, width):
    return f"{PREFIX}{width}/{os.path.splitext(name)[0]}.webp"


def wants_thumbnails(name):
    return name.lower().endswith(EXTENSIONS) and not name.startswith(PREFIX)


def render(fp):
    """{width: webp bytes} for one image file object."""
    img = Image.open(fp)
    img = ImageOps.exif_transpose(img)  # phone photos carry their rotation in EXIF
    has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
    img = img.convert('RGBA' if has_alpha else 'RGB')
    out = {}
    for width in WIDTHS:
        copy = img
        if img.width > width:
            copy = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
        buf = io.BytesIO()
        copy.save(buf, 'WEBP', quality=QUALITY, method=4)
        out[width] = buf.getvalue()
    return out


def make_thumbnails(storage, name, fp=None, replace=False):
    """
    Write every width for `name`. Reads `fp` when given (the upload still in
    memory), otherwise the stored file. Existing copies are kept unless `replace`.
    Returns how many were written.
    """
    sizes = None
    if fp is not None:
        try:
            fp.seek(0)
            sizes = render(fp)
        except (ValueError, OSError):
            sizes = None  # closed or not seekable after upload: read it back instead
    if sizes is None:
        with storage.open(name, 'rb') as f:
            sizes = render(f)
    written = 0
    for width, data in sizes.items():
        target = thumb_name(name, width)
        if storage.exists(target):
            if not replace:
                continue
            storage.delete(target)
        storage.save(target, ContentFile(data))
        written += 1
    return written


class MediaStorage(S3Boto3Storage):
    """Spaces storage for uploads that also writes the WebP copies (see above)."""

    def _save(self, name, content):
        name = super()._save(name, content)
        if wants_thumbnails(name):
            try:
                make_thumbnails(self, name, fp=content)
            except Exception:
                # The upload itself succeeded; `make_thumbnails` (the command)
                # can fill the gap, so never fail the user's request over this.
                logger.exception("thumbnails: could not make copies of %s", name)
        return name

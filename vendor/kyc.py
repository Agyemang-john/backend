"""
vendor/kyc.py
Identity documents sellers upload (ID card, student ID, proof of address,
business licence).

These are the most sensitive files we hold, so unlike product photos they:
  - are checked by content: PDF, JPEG, PNG or WebP only, up to 10 MB
  - are stored under a random name; the uploader's file name is discarded
    (it can leak personal details, and a name like "id.html" would otherwise
    decide how the file is served)
  - live in a private area of the bucket (ACL private) and are only reachable
    through signed links that expire after KYC_URL_TTL seconds

Files uploaded before this module existed were public; move them with
`python manage.py make_kyc_private`.
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.utils.deconstruct import deconstructible

KYC_MAX_BYTES = 10 * 1024 * 1024
KYC_URL_TTL = 10 * 60

# Leading bytes → extension. Checked against the file itself, never its name.
_SIGNATURES = (
    (b'%PDF-', 'pdf'),
    (b'\xff\xd8\xff', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
)


def _kind(head):
    for signature, ext in _SIGNATURES:
        if head.startswith(signature):
            return ext
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


def clean_kyc_upload(upload):
    """
    Validate an uploaded document and rename it after what it really is.
    Returns the same upload object; raises ValidationError when it isn't an
    accepted document.
    """
    if upload is None:
        return upload
    if upload.size > KYC_MAX_BYTES:
        raise ValidationError("Documents can be up to 10 MB.")
    upload.seek(0)
    ext = _kind(upload.read(16))
    upload.seek(0)
    if not ext:
        raise ValidationError("Upload the document as a PDF, JPG, PNG or WebP file.")
    upload.name = f"document.{ext}"
    return upload


_SAFE_EXTENSIONS = {'pdf', 'jpg', 'png', 'webp'}


@deconstructible
class KycUploadTo:
    """`kyc/<folder>/<random>.<ext>`. The name is random; the extension was set by clean_kyc_upload."""

    def __init__(self, folder):
        self.folder = folder

    def __call__(self, instance, filename):
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in _SAFE_EXTENSIONS:
            ext = 'bin'
        return f"kyc/{self.folder}/{uuid.uuid4().hex}.{ext}"

    def __eq__(self, other):
        return isinstance(other, KycUploadTo) and other.folder == self.folder


class KycFilesMixin:
    """For serializers that accept the document fields."""

    KYC_FIELDS = ('license', 'student_id', 'proof_of_address', 'government_issued_id')

    def to_internal_value(self, data):
        values = super().to_internal_value(data)
        errors = {}
        for field in self.KYC_FIELDS:
            if values.get(field):
                try:
                    clean_kyc_upload(values[field])
                except ValidationError as exc:
                    errors[field] = list(exc.messages)
        if errors:
            from rest_framework import serializers
            raise serializers.ValidationError(errors)
        return values


def kyc_storage():
    """Private storage in production (signed, expiring URLs); local disk otherwise."""
    if getattr(settings, 'ENV', '') == 'production':
        from storages.backends.s3boto3 import S3Boto3Storage
        return S3Boto3Storage(
            location='private',
            default_acl='private',
            querystring_auth=True,
            querystring_expire=KYC_URL_TTL,
            custom_domain=None,          # the CDN domain can't serve signed URLs
            file_overwrite=False,
        )
    return default_storage

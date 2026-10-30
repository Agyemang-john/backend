# utils/cleanup.py
import logging
from django.core.files.storage import default_storage
from product.models import Category, Sub_Category, Brand, Product, ProductImages, Variants, VariantImage
from vendor.models import Vendor, About
from core.models import HomeSlider, Banners

logger = logging.getLogger(__name__)

def cleanup_orphaned_files():
    """
    Identify and delete orphaned files in DigitalOcean Spaces that are not referenced by any model.
    Returns a tuple of (deleted_files, failed_files) for reporting.
    """
    deleted_files = []
    failed_files = []
    
    # Define models and their file fields
    model_fields = {
        Category: ['main_image', 'image'],
        Sub_Category: ['image'],
        Brand: ['image'],
        Product: ['image'],
        ProductImages: ['images'],
        Variants: ['image'],
        VariantImage: ['images'],
        About: ['profile_image', 'cover_image'],
        Vendor: ['license', 'student_id'],
        HomeSlider: ['image_desktop', 'image_mobile'],
        Banners: ['image'],
    }

    # Collect all referenced files
    referenced_files = set()
    for model, fields in model_fields.items():
        for instance in model.objects.iterator():  # Use iterator for memory efficiency
            for field in fields:
                file_field = getattr(instance, field, None)
                if file_field and file_field.name:
                    referenced_files.add(file_field.name)

    # List all files in storage, handling pagination
    all_files = set()
    try:
        # For S3Boto3Storage, use boto3 client for efficient listing
        from storages.backends.s3boto3 import S3Boto3Storage
        if isinstance(default_storage, S3Boto3Storage):
            import boto3
            s3 = boto3.client(
                's3',
                aws_access_key_id=default_storage.access_key,
                aws_secret_access_key=default_storage.secret_key,
                endpoint_url=default_storage.endpoint_url,
                region_name=default_storage.region_name,
            )
            paginator = s3.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=default_storage.bucket_name):
                for obj in page.get('Contents', []):
                    all_files.add(obj['Key'])
        else:
            # Fallback for non-S3 storage
            directories, files = default_storage.listdir('')
            for file in files:
                all_files.add(file)
            for directory in directories:
                _, sub_files = default_storage.listdir(directory)
                for file in sub_files:
                    all_files.add(f"{directory}/{file}")
    except Exception as e:
        logger.error(f"Failed to list files in storage: {e}")
        return deleted_files, [(None, str(e))]

    # Identify orphaned files
    orphaned_files = all_files - referenced_files

    # Delete orphaned files
    for file in orphaned_files:
        try:
            default_storage.delete(file)
            deleted_files.append(file)
            logger.info(f"Deleted orphaned file: {file}")
        except Exception as e:
            failed_files.append((file, str(e)))
            logger.error(f"Failed to delete orphaned file {file}: {e}")

    return deleted_files, failed_files
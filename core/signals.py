# signals.py
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.core.files.storage import default_storage
from product.models import Category, Sub_Category, Brand, Product, ProductImages, Variants, VariantImage, Main_Category
from vendor.models import Vendor, About
from core.models import HomeSlider, Banners
from django.core.cache import cache
import logging

logger = logging.getLogger(__name__)

@receiver(post_delete, sender=Category)
def delete_category_images(sender, instance, **kwargs):
    """Delete Category main_image and image from storage."""
    for field in ['main_image', 'image']:
        image_field = getattr(instance, field, None)
        if image_field:
            try:
                default_storage.delete(image_field.name)
                logger.info(f"Deleted {field} {image_field.name} for Category {instance.title}")
            except Exception as e:
                logger.error(f"Failed to delete {field} {image_field.name} for Category {instance.title}: {e}")

@receiver(post_delete, sender=Sub_Category)
def delete_sub_category_image(sender, instance, **kwargs):
    """Delete Sub_Category image from storage."""
    if instance.image:
        try:
            default_storage.delete(instance.image.name)
            logger.info(f"Deleted image {instance.image.name} for Sub_Category {instance.title}")
        except Exception as e:
            logger.error(f"Failed to delete image {instance.image.name} for Sub_Category {instance.title}: {e}")

@receiver(post_delete, sender=Brand)
def delete_brand_image(sender, instance, **kwargs):
    """Delete Brand image from storage."""
    if instance.image:
        try:
            default_storage.delete(instance.image.name)
            logger.info(f"Deleted image {instance.image.name} for Brand {instance.title}")
        except Exception as e:
            logger.error(f"Failed to delete image {instance.image.name} for Brand {instance.title}: {e}")

@receiver(post_delete, sender=Product)
def delete_product_image(sender, instance, **kwargs):
    """Delete Product image from storage."""
    if instance.image:
        try:
            default_storage.delete(instance.image.name)
            logger.info(f"Deleted image {instance.image.name} for Product {instance.title}")
        except Exception as e:
            logger.error(f"Failed to delete image {instance.image.name} for Product {instance.title}: {e}")

@receiver(post_delete, sender=ProductImages)
def delete_product_images(sender, instance, **kwargs):
    """Delete ProductImages images from storage."""
    if instance.images:
        try:
            default_storage.delete(instance.images.name)
            logger.info(f"Deleted image {instance.images.name} for ProductImages {instance.id}")
        except Exception as e:
            logger.error(f"Failed to delete image {instance.images.name} for ProductImages {instance.id}: {e}")

@receiver(post_delete, sender=Variants)
def delete_variant_image(sender, instance, **kwargs):
    """Delete Variants image from storage."""
    if instance.image:
        try:
            default_storage.delete(instance.image.name)
            logger.info(f"Deleted image {instance.image.name} for Variant {instance.title}")
        except Exception as e:
            logger.error(f"Failed to delete image {instance.image.name} for Variant {instance.title}: {e}")

@receiver(post_delete, sender=VariantImage)
def delete_variant_image_images(sender, instance, **kwargs):
    """Delete VariantImage images from storage."""
    if instance.images:
        try:
            default_storage.delete(instance.images.name)
            logger.info(f"Deleted image {instance.images.name} for VariantImage {instance.id}")
        except Exception as e:
            logger.error(f"Failed to delete image {instance.images.name} for VariantImage {instance.id}: {e}")

@receiver(post_delete, sender=About)
def delete_about_images(sender, instance, **kwargs):
    """Delete About profile_image and cover_image from storage."""
    for field in ['profile_image', 'cover_image']:
        image_field = getattr(instance, field, None)
        if image_field:
            try:
                default_storage.delete(image_field.name)
                logger.info(f"Deleted {field} {image_field.name} for About {instance.vendor.name}")
            except Exception as e:
                logger.error(f"Failed to delete {field} {image_field.name} for About {instance.vendor.name}: {e}")

@receiver(post_delete, sender=Vendor)
def delete_vendor_files(sender, instance, **kwargs):
    """Delete Vendor license and student_id files from storage."""
    for field in ['license', 'student_id']:
        file_field = getattr(instance, field, None)
        if file_field:
            try:
                default_storage.delete(file_field.name)
                logger.info(f"Deleted {field} {file_field.name} for Vendor {instance.name}")
            except Exception as e:
                logger.error(f"Failed to delete {field} {file_field.name} for Vendor {instance.name}: {e}")

@receiver(post_delete, sender=HomeSlider)
def delete_slider_images(sender, instance, **kwargs):
    """Delete HomeSlider image_desktop and image_mobile from storage."""
    for field in ['image_desktop', 'image_mobile']:
        file_field = getattr(instance, field, None)
        if file_field:
            try:
                default_storage.delete(file_field.name)
                logger.info(f"Deleted {field} {file_field.name} for HomeSlider {instance.title}")
            except Exception as e:
                logger.error(f"Failed to delete {field} {file_field.name} for HomeSlider {instance.title}: {e}")

@receiver(post_delete, sender=Banners)
def delete_banner_images(sender, instance, **kwargs):
    """Delete Banners image from storage."""
    if instance.image:
        try:
            default_storage.delete(instance.image.name)
            logger.info(f"Deleted image {instance.image.name} for Banners {instance.title}")
        except Exception as e:
            logger.error(f"Failed to delete image {instance.image.name} for Banners {instance.title}: {e}")

# CACHE
@receiver([post_save, post_delete], sender=Category)
def invalidate_top_engaged_category_cache(sender, instance, **kwargs):
    cache_key = 'top_engaged_category'
    cache.delete(cache_key)

@receiver([post_save, post_delete], sender=Category)
def invalidate_category_cache(sender, instance, **kwargs):
    cache_key = f'category_detail_{instance.slug}'
    cache.delete(cache_key)

@receiver([post_save, post_delete], sender=Main_Category)
def invalidate_main_category_cache(sender, instance, **kwargs):
    cache_key = f'main_categories_with_categories'
    cache.delete(cache_key)

@receiver([post_save, post_delete], sender=HomeSlider)
def invalidate_home_slider_cache(sender, instance, **kwargs):
    # Invalidate cache for all currencies (or use a more specific approach if needed)
    currencies = ['GHS', 'USD', 'EUR']  # Adjust based on supported currencies
    for currency in currencies:
        cache_key = f'home_sliders_{currency}'
        cache.delete(cache_key)

@receiver([post_save, post_delete], sender=Banners)
def invalidate_banners_cache(sender, instance, **kwargs):
    cache_key = 'banners'
    cache.delete(cache_key)


# tasks.py
from celery import shared_task
from .cleanup import cleanup_orphaned_files
import logging

logger = logging.getLogger(__name__)

@shared_task
def cleanup_orphaned_files_task():
    """
    Celery task to clean up orphaned files in storage.
    Runs weekly at dawn.
    """
    logger.info("Starting orphaned files cleanup task")
    deleted_files, failed_files = cleanup_orphaned_files()
    logger.info(f"Cleanup completed. Deleted {len(deleted_files)} files. Failed {len(failed_files)} files.")
    if failed_files:
        logger.warning(f"Failed to delete files: {failed_files}")
    return {
        "deleted_files": deleted_files,
        "failed_files": failed_files,
    }
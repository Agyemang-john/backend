"""
Make the WebP copies (core/thumbnails.py) for images uploaded before they were
made on upload. Safe to re-run: files that already have every copy are skipped.

    python manage.py make_thumbnails --dry-run
    python manage.py make_thumbnails --cache      # also set the 1-year cache header on originals

Run it after deploying the backend and before deploying the storefront's image
loader, or cards for older images point at copies that don't exist yet.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from storages.backends.s3boto3 import S3Boto3Storage

from core.thumbnails import LONG_CACHE, PREFIX, WIDTHS, make_thumbnails, thumb_name, wants_thumbnails


class Command(BaseCommand):
    help = 'Make missing WebP thumbnails for uploaded images.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only count what is missing')
        parser.add_argument('--replace', action='store_true', help='Remake copies that already exist')
        parser.add_argument('--cache', action='store_true',
                            help='Also give originals the 1-year Cache-Control header (Spaces only)')
        parser.add_argument('--workers', type=int, default=8)

    def handle(self, *args, **options):
        storage = default_storage
        s3 = isinstance(storage, S3Boto3Storage)
        names = self._list_s3(storage) if s3 else self._list_local()
        existing = set(names)
        originals = sorted(n for n in names if wants_thumbnails(n))
        todo = [n for n in originals
                if options['replace'] or any(thumb_name(n, w) not in existing for w in WIDTHS)]
        self.stdout.write(f"{len(originals)} images, {len(todo)} need thumbnails.")
        if options['dry_run']:
            return

        def work(name):
            # Thumbnails are public, so never make them of a private file (e.g. an
            # identity document uploaded before vendor/kyc.py moved them).
            if s3 and not self._is_public(storage, name):
                return 0
            made = make_thumbnails(storage, name, replace=options['replace']) if name in todo_set else 0
            if s3 and options['cache']:
                self._set_cache(storage, name)
            return made

        todo_set = set(todo)
        targets = originals if (s3 and options['cache']) else todo
        done = failed = made = 0
        with ThreadPoolExecutor(max_workers=options['workers']) as pool:
            futures = {pool.submit(work, n): n for n in targets}
            for future in as_completed(futures):
                try:
                    made += future.result()
                    done += 1
                except Exception as exc:
                    failed += 1
                    self.stderr.write(f"  failed: {futures[future]}: {exc}")
                if (done + failed) % 100 == 0:
                    self.stdout.write(f"  {done + failed}/{len(targets)}")
        self.stdout.write(self.style.SUCCESS(f"Done: {done} files, {made} thumbnails written, {failed} failed."))

    def _list_s3(self, storage):
        """Names relative to the storage location (e.g. 'vendors/vendor_2/w1.jpg')."""
        client = storage.connection.meta.client
        prefix = f"{storage.location.strip('/')}/" if storage.location else ''
        names = []
        for page in client.get_paginator('list_objects_v2').paginate(Bucket=storage.bucket_name, Prefix=prefix):
            for obj in page.get('Contents', []):
                names.append(obj['Key'][len(prefix):])
        return names

    def _list_local(self):
        root = settings.MEDIA_ROOT
        names = []
        for dirpath, _, files in os.walk(root):
            for f in files:
                names.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, '/'))
        return names

    def _is_public(self, storage, name):
        acl = storage.connection.meta.client.get_object_acl(
            Bucket=storage.bucket_name, Key=storage._normalize_name(name))
        return any(g.get('Grantee', {}).get('URI', '').endswith('/global/AllUsers') and g.get('Permission') == 'READ'
                   for g in acl.get('Grants', []))

    def _set_cache(self, storage, name):
        """
        Copy the object onto itself with the long Cache-Control (S3 can't edit
        headers in place). Only called for public objects, so public-read keeps
        the ACL as it was.
        """
        if name.startswith(PREFIX):
            return
        client = storage.connection.meta.client
        key = storage._normalize_name(name)
        head = client.head_object(Bucket=storage.bucket_name, Key=key)
        if head.get('CacheControl') == LONG_CACHE:
            return
        client.copy_object(
            Bucket=storage.bucket_name, Key=key,
            CopySource={'Bucket': storage.bucket_name, 'Key': key},
            MetadataDirective='REPLACE',
            CacheControl=LONG_CACHE,
            ContentType=head.get('ContentType') or 'application/octet-stream',
            ACL='public-read',
            Metadata=head.get('Metadata', {}),
        )

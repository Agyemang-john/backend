"""
core/tests_thumbnails.py
WebP copies for the storefront's image loader (core/thumbnails.py).

Run with:  DB_HOST=localhost python manage.py test core.tests_thumbnails --keepdb
"""

import io
import shutil
import tempfile
from unittest import mock

from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from PIL import Image

from core import thumbnails


def image_bytes(width, height, mode='RGB', fmt='JPEG'):
    buf = io.BytesIO()
    Image.new(mode, (width, height), (200, 30, 30, 128) if mode == 'RGBA' else (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


class ThumbnailTests(SimpleTestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.storage = FileSystemStorage(location=self.root)

    def test_names_match_the_frontend_loader(self):
        self.assertEqual(thumbnails.thumb_name('vendors/vendor_2/w1.jpg', 384), 'thumbs/384/vendors/vendor_2/w1.webp')
        self.assertTrue(thumbnails.wants_thumbnails('vendors/a.PNG'))
        self.assertFalse(thumbnails.wants_thumbnails('vendors/a.gif'))
        self.assertFalse(thumbnails.wants_thumbnails('thumbs/384/vendors/a.webp'))

    def test_every_width_exists_and_never_upscales(self):
        sizes = thumbnails.render(io.BytesIO(image_bytes(800, 400)))
        self.assertEqual(set(sizes), set(thumbnails.WIDTHS))
        dims = {w: Image.open(io.BytesIO(b)).size for w, b in sizes.items()}
        self.assertEqual(dims[256], (256, 128))
        self.assertEqual(dims[640], (640, 320))
        self.assertEqual(dims[1080], (800, 400))  # smaller than the slot: kept as is
        self.assertEqual(Image.open(io.BytesIO(sizes[384])).format, 'WEBP')

    def test_transparency_is_kept(self):
        sizes = thumbnails.render(io.BytesIO(image_bytes(300, 300, 'RGBA', 'PNG')))
        self.assertEqual(Image.open(io.BytesIO(sizes[256])).mode, 'RGBA')

    def test_make_thumbnails_writes_once(self):
        name = self.storage.save('vendors/v1/shoe.jpg', io.BytesIO(image_bytes(1200, 1200)))
        self.assertEqual(thumbnails.make_thumbnails(self.storage, name), 4)
        for w in thumbnails.WIDTHS:
            self.assertTrue(self.storage.exists(thumbnails.thumb_name(name, w)))
        self.assertEqual(thumbnails.make_thumbnails(self.storage, name), 0)
        self.assertEqual(thumbnails.make_thumbnails(self.storage, name, replace=True), 4)

    def test_upload_makes_copies_and_never_fails_the_upload(self):
        storage = thumbnails.MediaStorage.__new__(thumbnails.MediaStorage)
        with mock.patch('storages.backends.s3boto3.S3Boto3Storage._save', return_value='vendors/x.jpg'), \
                mock.patch.object(thumbnails, 'make_thumbnails') as make:
            self.assertEqual(storage._save('vendors/x.jpg', io.BytesIO(b'..')), 'vendors/x.jpg')
            make.assert_called_once()
            make.side_effect = OSError('broken image')
            self.assertEqual(storage._save('vendors/x.jpg', io.BytesIO(b'..')), 'vendors/x.jpg')
        with mock.patch('storages.backends.s3boto3.S3Boto3Storage._save', return_value='thumbs/256/x.webp'), \
                mock.patch.object(thumbnails, 'make_thumbnails') as make:
            storage._save('thumbs/256/x.webp', io.BytesIO(b'..'))
            make.assert_not_called()  # copies don't get copies

    def test_command_fills_the_gaps(self):
        self.storage.save('vendors/a.jpg', io.BytesIO(image_bytes(500, 500)))
        self.storage.save('vendors/b.png', io.BytesIO(image_bytes(100, 50, 'RGBA', 'PNG')))
        self.storage.save('docs/readme.txt', io.BytesIO(b'hi'))
        out = io.StringIO()
        with override_settings(MEDIA_ROOT=self.root), \
                mock.patch('core.management.commands.make_thumbnails.default_storage', self.storage):
            call_command('make_thumbnails', stdout=out)
            self.assertIn('2 images, 2 need thumbnails', out.getvalue())
            out = io.StringIO()
            call_command('make_thumbnails', '--dry-run', stdout=out)
            self.assertIn('2 images, 0 need thumbnails', out.getvalue())
        self.assertTrue(self.storage.exists('thumbs/1080/vendors/b.webp'))

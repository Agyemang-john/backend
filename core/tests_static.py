"""
core/tests_static.py

Production serves static files under content-hashed names (core/storage.py).
These tests check the storage is configured the way that relies on, and that
the admin renders with hashed URLs, including Jazzmin's reference to a static
*directory*, which a strict manifest would turn into a 500 on every page.

Run: DB_HOST=localhost python manage.py test core.tests_static --keepdb
"""

import shutil
import tempfile

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from core.storage import StaticStorage
from userauths.models import User


class LenientManifestStorage(ManifestStaticFilesStorage):
    """Local-disk twin of core.storage.StaticStorage's manifest behaviour."""
    manifest_strict = StaticStorage.manifest_strict


class StaticStorageSettingsTests(TestCase):

    def test_production_storage_configuration(self):
        # Asserted on the class: instantiating it reads staticfiles.json from S3.
        # Class attributes take precedence over the AWS_* settings in
        # storages.base.BaseStorage.__init__, so these are what production uses.
        self.assertEqual(StaticStorage.location, 'static')
        self.assertFalse(StaticStorage.manifest_strict)
        self.assertTrue(StaticStorage.file_overwrite)        # not the media setting (False)
        self.assertFalse(StaticStorage.querystring_auth)
        self.assertIn('immutable', StaticStorage.object_parameters['CacheControl'])


class HashedStaticAdminTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.static_root = tempfile.mkdtemp()
        cls._settings = override_settings(
            DEBUG=False,
            STATIC_ROOT=cls.static_root,
            STATIC_URL='/static/',
            STORAGES={
                'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {'BACKEND': 'core.tests_static.LenientManifestStorage'},
            },
            CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
            SECURE_SSL_REDIRECT=False,
        )
        cls._settings.enable()
        call_command('collectstatic', interactive=False, verbosity=0)

    @classmethod
    def tearDownClass(cls):
        cls._settings.disable()
        shutil.rmtree(cls.static_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        cache.clear()

    def test_admin_pages_use_hashed_assets(self):
        login = self.client.get(reverse('admin:login'))
        self.assertEqual(login.status_code, 200)
        self.assertRegex(login.content.decode(), r'adminlte\.min\.[0-9a-f]{12}\.css')

        staff = User.objects.create(email='staff@example.com', first_name='S', last_name='T',
                                    phone='+233200000001', is_staff=True, is_superuser=True, is_active=True)
        self.client.force_login(staff)
        page = self.client.get(reverse('admin:index'))
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertRegex(html, r'jazzmin/css/main\.[0-9a-f]{12}\.css')
        self.assertIn('/static/vendor/bootswatch', html)  # the directory reference, unhashed, not a 500

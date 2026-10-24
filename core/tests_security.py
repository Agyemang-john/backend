"""
core/tests_security.py
Uploads and seller-written HTML can't carry anything a browser would run.

Run with:  DB_HOST=localhost python manage.py test core.tests_security --keepdb
"""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from core.html_sanitize import clean_html
from product.review_media import MediaError, check_video
from vendor.kyc import KycUploadTo, clean_kyc_upload

MP4_HEAD = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00'


class HtmlSanitizeTests(SimpleTestCase):

    def test_scripts_handlers_and_js_links_are_removed(self):
        dirty = ('<p onclick="steal()">Nice kettle<script>fetch("/x")</script></p>'
                 '<img src="x" onerror="alert(1)"><a href="javascript:alert(1)">more</a>'
                 '<iframe src="https://evil.example"></iframe><svg onload="alert(1)"></svg>'
                 '<p style="position:fixed">overlay</p>')
        clean = clean_html(dirty)
        for bad in ('script', 'onclick', 'onerror', 'javascript:', 'iframe', 'svg', 'onload', 'style='):
            self.assertNotIn(bad, clean)
        self.assertIn('Nice kettle', clean)

    def test_editor_formatting_survives(self):
        html = ('<h2>Specs</h2><ul><li><strong>Power:</strong> 2kW</li></ul>'
                '<table><tbody><tr><td>Size</td><td>1.7L</td></tr></tbody></table>'
                '<a href="https://negromart.com/help">Help</a>')
        clean = clean_html(html)
        for good in ('<h2>', '<ul>', '<strong>', '<table>', '<td>', 'href="https://negromart.com/help"'):
            self.assertIn(good, clean)

    def test_empty_values_pass_through(self):
        self.assertIsNone(clean_html(None))
        self.assertEqual(clean_html(''), '')


class VideoUploadTests(SimpleTestCase):

    def test_name_comes_from_content_not_upload(self):
        upload = SimpleUploadedFile('clip.html', MP4_HEAD + b'rest', content_type='text/html')
        name = check_video(upload)
        self.assertTrue(name.endswith('.mp4'))
        self.assertNotIn('clip', name)

    def test_html_pretending_to_be_video_is_refused(self):
        upload = SimpleUploadedFile('clip.mp4', b'<html><script>alert(1)</script>', content_type='video/mp4')
        with self.assertRaises(MediaError):
            check_video(upload)


class KycUploadTests(SimpleTestCase):

    def test_pdf_and_images_accepted_and_renamed(self):
        for head, ext in ((b'%PDF-1.7\n', 'pdf'), (b'\xff\xd8\xff\xe0', 'jpg'),
                          (b'\x89PNG\r\n\x1a\n', 'png'), (b'RIFF\x00\x00\x00\x00WEBPVP8 ', 'webp')):
            upload = SimpleUploadedFile('Kofi Mensah passport.html', head + b'\x00' * 32)
            clean_kyc_upload(upload)
            self.assertEqual(upload.name, f'document.{ext}')
            path = KycUploadTo('id')(None, upload.name)
            self.assertTrue(path.startswith('kyc/id/') and path.endswith(f'.{ext}'))
            self.assertNotIn('Kofi', path)

    def test_other_files_refused(self):
        for content in (b'<html></html>', b'MZ\x90\x00 exe', b'PK\x03\x04 zip'):
            with self.assertRaises(ValidationError):
                clean_kyc_upload(SimpleUploadedFile('id.pdf', content))

    def test_size_cap(self):
        big = SimpleUploadedFile('id.pdf', b'%PDF-' + b'0' * (10 * 1024 * 1024 + 1))
        with self.assertRaises(ValidationError):
            clean_kyc_upload(big)

    def test_unknown_extension_never_kept(self):
        self.assertTrue(KycUploadTo('id')(None, 'x.html').endswith('.bin'))

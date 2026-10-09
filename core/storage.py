"""
Static file storage for production (DigitalOcean Spaces behind its CDN).

Files are uploaded under content-hashed names (adminlte.min.64eb91d6ceb8.css)
and templates reference those names through the staticfiles.json manifest that
collectstatic writes. A new version of a file therefore gets a new URL, so a
browser or CDN edge holding the old copy can never serve it with new HTML. With
plain names, upgrading django-jazzmin (Bootstrap 4 -> 5) left browsers on the
cached old CSS for up to a day and the admin rendered unstyled.

collectstatic must run on every deploy (entrypoint.sh does) so the manifest
matches the code.
"""

from storages.backends.s3 import S3ManifestStaticStorage


class StaticStorage(S3ManifestStaticStorage):
    location = 'static'

    # AWS_S3_FILE_OVERWRITE is False for user uploads (media); static files
    # must keep their exact names or the manifest points at the wrong object.
    file_overwrite = True

    # Hashed names never change content, so they can be cached for a year.
    object_parameters = {'CacheControl': 'public, max-age=31536000, immutable'}

    # Jazzmin's base template does {% static 'vendor/bootswatch' %} (a
    # directory, never in the manifest) on every admin page; strict mode would
    # turn that into a 500. Unknown paths fall back to their unhashed URL.
    manifest_strict = False

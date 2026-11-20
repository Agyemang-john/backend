"""
vendor/remote_images.py
Download a product photo from a link a seller typed into a bulk-upload sheet.

The server fetches URLs chosen by someone else, so:
  - only http(s) on the default ports, at most 3 redirects, each hop re-checked;
  - every address the host resolves to must be public (no localhost, private
    networks, link-local/metadata addresses), and the connected peer is
    checked again after connecting;
  - at most IMAGE_MAX_BYTES are read, with short timeouts;
  - the bytes must open as a JPG/PNG/WebP photo and are re-encoded to JPEG
    (drops EXIF/GPS and anything hidden in the file), like review photos.

Share links from Google Drive and Dropbox are turned into direct downloads,
because that is where most sellers keep their photos.
"""
import ipaddress
import re
import socket
import uuid
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_MAX_BYTES = 10 * 1024 * 1024
MIN_SIDE = 400            # same floor as the seller product form
MAX_SIDE = 1800
MAX_REDIRECTS = 3
TIMEOUT = (5, 15)         # connect, read
_FORMATS = {'JPEG', 'PNG', 'WEBP', 'MPO'}
_HEADERS = {'User-Agent': 'NegromartBulkUpload/1.0 (product photo import)', 'Accept': 'image/*'}


class RemoteImageError(ValueError):
    """Message is safe to show to the seller."""


def direct_link(url):
    """Google Drive / Dropbox share links → links that return the file itself."""
    url = url.strip()
    drive = re.match(r'https?://drive\.google\.com/(?:file/d/([\w-]+)|open\?id=([\w-]+))', url)
    if drive:
        return f'https://drive.google.com/uc?export=download&id={drive.group(1) or drive.group(2)}'
    if re.match(r'https?://(www\.)?dropbox\.com/', url):
        url = re.sub(r'([?&])dl=0', r'\1raw=1', url)
        if 'raw=1' not in url:
            url += ('&' if '?' in url else '?') + 'raw=1'
    return url


def _check_host(url):
    parts = urlparse(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise RemoteImageError('Use a link that starts with http:// or https://.')
    if parts.port not in (None, 80, 443):
        raise RemoteImageError('Photo links must use the normal web ports.')
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise RemoteImageError('The website in this photo link could not be found.')
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise RemoteImageError('This photo link points to a private address.')


def _check_peer(response):
    try:
        sock = response.raw.connection.sock
        peer = sock.getpeername()[0]
    except Exception:       # pragma: no cover - transport without a socket (tests)
        return
    if not ipaddress.ip_address(peer).is_global:
        raise RemoteImageError('This photo link points to a private address.')


def _download(url):
    for _ in range(MAX_REDIRECTS + 1):
        _check_host(url)
        try:
            response = requests.get(url, headers=_HEADERS, timeout=TIMEOUT, stream=True, allow_redirects=False)
        except requests.RequestException:
            raise RemoteImageError('The photo could not be downloaded (the website did not answer).')
        with response:
            _check_peer(response)
            if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            if response.status_code != 200:
                raise RemoteImageError(f'The photo could not be downloaded (the website answered {response.status_code}).')
            declared = int(response.headers.get('Content-Length') or 0)
            if declared > IMAGE_MAX_BYTES:
                raise RemoteImageError('The photo is larger than 10 MB.')
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > IMAGE_MAX_BYTES:
                    raise RemoteImageError('The photo is larger than 10 MB.')
            return bytes(data)
    raise RemoteImageError('The photo link redirects too many times.')


def to_product_photo(data):
    """Bytes of an uploaded or downloaded photo → (ContentFile JPEG, (w, h)). Raises RemoteImageError."""
    try:
        with Image.open(BytesIO(data)) as original:
            if original.format not in _FORMATS:
                raise RemoteImageError('The link is not a JPG, PNG or WebP photo.')
            original.load()
            img = ImageOps.exif_transpose(original)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise RemoteImageError('The link did not lead to a photo. Use a direct link to the image file.')
    if min(img.size) < MIN_SIDE:
        raise RemoteImageError(f'The photo is too small ({img.size[0]}×{img.size[1]}); use at least {MIN_SIDE}×{MIN_SIDE} pixels.')
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    if img.mode not in ('RGB', 'L'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        background.paste(img, mask=img.convert('RGBA').split()[-1])
        img = background
    buffer = BytesIO()
    img.convert('RGB').save(buffer, format='JPEG', quality=88, optimize=True, progressive=True)
    return ContentFile(buffer.getvalue(), name=f'{uuid.uuid4().hex}.jpg'), img.size


def fetch_photo(url):
    """Download and clean one photo. Returns a ContentFile ready for an ImageField."""
    photo, _ = to_product_photo(_download(direct_link(url)))
    return photo

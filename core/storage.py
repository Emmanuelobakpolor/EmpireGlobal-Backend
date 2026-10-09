"""Cloudinary storage for uploaded files (payment receipts, application documents, display pictures).

Enabled by setting CLOUDINARY_URL (cloudinary://<api_key>:<api_secret>@<cloud_name>). Every file is
uploaded as an *authenticated* asset, so a plain Cloudinary link can't open it:

- receipts and documents are streamed to the owner or an admin by the API (`open` downloads them
  through a signed, short-lived API URL), exactly as with local storage;
- display pictures are delivered through signed, face-cropped image URLs (`avatar_url`).

File names in the database stay the same as with local storage (e.g. `receipts/2026/10/<uuid>.pdf`);
they map to public IDs under CLOUDINARY_FOLDER.
"""

import logging
import time
import urllib.request
from urllib.parse import unquote, urlparse

import cloudinary
import cloudinary.uploader
import cloudinary.utils
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
DELIVERY_TYPE = 'authenticated'
# How long a server-side download link stays valid
DOWNLOAD_LINK_SECONDS = 60
AVATAR_TRANSFORMATION = [
    {'width': 256, 'height': 256, 'crop': 'fill', 'gravity': 'face'},
    {'fetch_format': 'auto', 'quality': 'auto'},
]


@deconstructible
class CloudinaryStorage(Storage):
    def __init__(self, folder=None, timeout=20):
        self.folder = (folder if folder is not None else settings.CLOUDINARY_FOLDER).strip('/')
        self.timeout = timeout
        if settings.CLOUDINARY_URL:
            # cloudinary://<api_key>:<api_secret>@<cloud_name>
            parsed = urlparse(settings.CLOUDINARY_URL)
            cloudinary.config(cloud_name=parsed.hostname, api_key=unquote(parsed.username or ''),
                              api_secret=unquote(parsed.password or ''), secure=True)

    def _locate(self, name):
        """(resource_type, public_id, format) for a stored name.

        Images keep their extension as the delivery format; PDFs are stored as raw files,
        whose public ID includes the extension.
        """
        name = name.replace('\\', '/').lstrip('/')
        prefixed = f'{self.folder}/{name}' if self.folder else name
        stem, dot, ext = prefixed.rpartition('.')
        ext = ext.lower()
        if dot and ext in IMAGE_EXTENSIONS:
            return 'image', stem, ext
        return 'raw', prefixed, None

    def _save(self, name, content):
        resource_type, public_id, _ = self._locate(name)
        content.seek(0)
        cloudinary.uploader.upload(
            content,
            public_id=public_id,
            resource_type=resource_type,
            type=DELIVERY_TYPE,
            overwrite=False,
            unique_filename=False,
            use_filename=False,
        )
        return name.replace('\\', '/')

    def _open(self, name, mode='rb'):
        resource_type, public_id, fmt = self._locate(name)
        url = cloudinary.utils.private_download_url(
            public_id, fmt, resource_type=resource_type, type=DELIVERY_TYPE,
            expires_at=int(time.time()) + DOWNLOAD_LINK_SECONDS,
        )
        with urllib.request.urlopen(url, timeout=self.timeout) as response:
            file = ContentFile(response.read())
        file.name = name
        return file

    def delete(self, name):
        if not name:
            return
        resource_type, public_id, _ = self._locate(name)
        try:
            cloudinary.uploader.destroy(public_id, resource_type=resource_type, type=DELIVERY_TYPE, invalidate=True)
        except Exception as exc:  # A leftover file must never break the action that replaced it
            logger.warning('Could not delete %s from Cloudinary: %s', public_id, type(exc).__name__)

    def exists(self, name):
        # Names are random UUIDs, so a clash is not a real concern; skip the API round trip
        return False

    def url(self, name):
        return self.signed_url(name)

    def signed_url(self, name, transformation=None):
        resource_type, public_id, fmt = self._locate(name)
        options = {'resource_type': resource_type, 'type': DELIVERY_TYPE, 'sign_url': True, 'secure': True}
        if fmt:
            options['format'] = fmt
        if transformation:
            options['transformation'] = transformation
        return cloudinary.utils.cloudinary_url(public_id, **options)[0]

    def avatar_url(self, name):
        return self.signed_url(name, AVATAR_TRANSFORMATION)

    def size(self, name):
        raise NotImplementedError('File sizes are recorded in the database.')

"""Checks for uploaded files (payment receipts, application documents) and private serving."""

import mimetypes

from django.http import FileResponse
from rest_framework import serializers

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Checked against the file's first bytes, not its name or the browser's claimed type
SIGNATURES = {
    'pdf': [b'%PDF'],
    'png': [b'\x89PNG\r\n\x1a\n'],
    'jpg': [b'\xff\xd8\xff'],
    'jpeg': [b'\xff\xd8\xff'],
    'webp': [b'RIFF'],
}


def validate_upload(file):
    if file.size > MAX_UPLOAD_BYTES:
        raise serializers.ValidationError('Files must be 5 MB or smaller.')
    ext = file.name.rsplit('.', 1)[-1].lower() if '.' in file.name else ''
    signatures = SIGNATURES.get(ext)
    if not signatures:
        raise serializers.ValidationError('Upload a JPG, PNG, WebP or PDF file.')
    head = file.read(16)
    file.seek(0)
    if not any(head.startswith(sig) for sig in signatures) or (ext == 'webp' and head[8:12] != b'WEBP'):
        raise serializers.ValidationError("This file doesn't look like a valid image or PDF.")
    return file


def serve_private_file(field, download_name):
    """Stream a stored file inline, with headers that stop the browser treating it as anything else."""
    content_type = mimetypes.guess_type(field.name)[0] or 'application/octet-stream'
    response = FileResponse(field.open('rb'), content_type=content_type)
    response['Content-Disposition'] = f'inline; filename="{download_name.replace(chr(34), "")}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response

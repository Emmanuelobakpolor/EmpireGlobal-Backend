"""Google sign-in: turns the authorization code from the Google popup into a verified identity.

The browser opens Google's popup (Google Identity Services, code flow) and sends us the one-time
code. We exchange it with Google for an ID token over a direct TLS connection, using our client
secret, so the token's contents can be trusted without checking its signature (OpenID Connect
Core 3.1.3.7); we still check who it was issued for, by whom, and that it hasn't expired.
"""

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TOKEN_URL = 'https://oauth2.googleapis.com/token'
ISSUERS = {'accounts.google.com', 'https://accounts.google.com'}


class GoogleAuthError(Exception):
    pass


def is_enabled():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def _post_form(url, fields, timeout=10):
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _decode_payload(id_token):
    try:
        payload = id_token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as exc:
        raise GoogleAuthError('Google returned an unreadable sign-in token.') from exc


def exchange_code(code):
    """Return the verified Google identity: {'sub', 'email', 'name'}."""
    try:
        tokens = _post_form(TOKEN_URL, {
            'code': code,
            'client_id': settings.GOOGLE_CLIENT_ID,
            'client_secret': settings.GOOGLE_CLIENT_SECRET,
            # Codes from the Google Identity Services popup are bound to this special redirect URI
            'redirect_uri': 'postmessage',
            'grant_type': 'authorization_code',
        })
    except urllib.error.HTTPError as exc:
        # 400 means the code was invalid, expired or already used; log the status only
        logger.warning('Google rejected a sign-in code: %s', exc.code)
        raise GoogleAuthError('Google sign-in failed or expired. Please try again.') from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.error('Could not reach Google to finish sign-in: %s', type(exc).__name__)
        raise GoogleAuthError('Could not reach Google. Please try again.') from exc

    claims = _decode_payload(tokens.get('id_token', ''))
    if claims.get('aud') != settings.GOOGLE_CLIENT_ID or claims.get('iss') not in ISSUERS:
        raise GoogleAuthError('This Google sign-in was not meant for Empire Global.')
    if claims.get('exp', 0) < time.time():
        raise GoogleAuthError('Google sign-in expired. Please try again.')
    if not claims.get('sub') or not claims.get('email'):
        raise GoogleAuthError('Google did not share your email address.')
    if not claims.get('email_verified'):
        raise GoogleAuthError('Your Google email address is not verified.')
    return {
        'sub': str(claims['sub']),
        'email': claims['email'].strip().lower(),
        'name': (claims.get('name') or '').strip()[:150],
    }

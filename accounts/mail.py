import json
import logging
import sys
import threading
import urllib.error
import urllib.request

from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class ConsoleEmailBackend(BaseEmailBackend):
    """Development backend: prints each email as plain, readable text.

    Django's own console backend prints the encoded MIME message, which mangles
    links (e.g. `=` becomes `=3D`). This prints the subject, recipients and body
    as-is so codes and reset links can be copied straight from the terminal.
    """

    def __init__(self, *, stream=None, **kwargs):
        super().__init__(**kwargs)
        self.stream = stream or sys.stdout
        self._lock = threading.RLock()

    def send_messages(self, email_messages):
        with self._lock:
            for message in email_messages:
                rule = '=' * 72
                self._write('\n'.join([
                    '',
                    rule,
                    'EMAIL (not sent - EMAIL_PROVIDER=console)',
                    f'To:      {", ".join(message.to)}',
                    f'From:    {message.from_email}',
                    f'Subject: {message.subject}',
                    '-' * 72,
                    message.body.rstrip(),
                    rule,
                    '',
                ]))
            self.stream.flush()
        return len(email_messages)

    def _write(self, text):
        # Windows consoles often can't print characters like ₦; show "NGN" rather than failing
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, 'encoding', None) or 'ascii'
            safe = text.replace('₦', 'NGN ').encode(encoding, 'replace').decode(encoding)
            self.stream.write(safe)


class ResendEmailBackend(BaseEmailBackend):
    """Sends mail through the Resend HTTP API (https://resend.com/docs/api-reference/emails/send-email).

    Enabled with EMAIL_PROVIDER=resend; the API key comes from RESEND_API_KEY via
    MAILERS['default']['OPTIONS'] and never leaves the server.
    """

    api_url = 'https://api.resend.com/emails'

    def __init__(self, *, api_key, timeout=10, fail_silently=False, **kwargs):
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError('EMAIL_PROVIDER is "resend" but RESEND_API_KEY is not set.')
        self.api_key = api_key
        self.timeout = timeout
        self.fail_silently = fail_silently

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            try:
                self._send(message)
                sent += 1
            except (urllib.error.URLError, OSError) as exc:
                # Log the status only: the response body can echo recipient details
                logger.error('Resend rejected an email: %s', getattr(exc, 'code', type(exc).__name__))
                if not self.fail_silently:
                    raise
        return sent

    def _send(self, message):
        payload = {
            'from': message.from_email,
            'to': message.to,
            'subject': message.subject,
            'text': message.body,
        }
        if message.cc:
            payload['cc'] = message.cc
        if message.bcc:
            payload['bcc'] = message.bcc
        if message.reply_to:
            payload['reply_to'] = message.reply_to
        for content, mimetype in getattr(message, 'alternatives', []):
            if mimetype == 'text/html':
                payload['html'] = content

        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode(),
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
                'User-Agent': 'empire-global-backend',
            },
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()

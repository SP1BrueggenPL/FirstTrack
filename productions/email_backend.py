import base64
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 3

# Środowisko testowe korzysta z darmowej domeny Azure (AzureManagedDomain),
# która ma bardzo niski limit wysyłek (Essentials w portalu: 5/min, 10/h -
# w praktyce throttling potrafi trwać dużo dłużej niż godzinę). Lokalny
# licznik z marginesem bezpieczeństwa chroni resztę puli przed dobiciem jej
# przez nieudane próby - powtarzanie zapytania po 429 też liczy się do
# limitu i tylko pogarsza throttling. Po przejściu na zweryfikowaną domenę
# własną te limity znikają i licznik przestaje mieć znaczenie.
RATE_LIMIT_PER_MINUTE = 4
RATE_LIMIT_PER_HOUR = 8


def _rate_limited():
    minute_key = f'acs_email_sends_{int(time.time() // 60)}'
    hour_key = f'acs_email_sends_h{int(time.time() // 3600)}'
    return (cache.get(minute_key, 0) >= RATE_LIMIT_PER_MINUTE
            or cache.get(hour_key, 0) >= RATE_LIMIT_PER_HOUR)


def _record_send_attempt():
    minute_key = f'acs_email_sends_{int(time.time() // 60)}'
    hour_key = f'acs_email_sends_h{int(time.time() // 3600)}'
    cache.set(minute_key, cache.get(minute_key, 0) + 1, 65)
    cache.set(hour_key, cache.get(hour_key, 0) + 1, 3665)


def _build_attachments(message):
    attachments = []
    for attachment in message.attachments:
        if isinstance(attachment, tuple):
            filename, content, mimetype = attachment
        else:
            # MIMEBase (rzadki przypadek - np. attach_alternative) - pomijamy,
            # ACS Email nie ma bezpośredniego odpowiednika tej ścieżki Django.
            continue
        content_bytes = content.encode('utf-8') if isinstance(content, str) else content
        attachments.append({
            'name': filename,
            'contentType': mimetype or 'application/octet-stream',
            'contentInBase64': base64.b64encode(content_bytes).decode('ascii'),
        })
    return attachments


class AzureCommunicationEmailBackend(BaseEmailBackend):
    """Wysyłka maili przez Azure Communication Services – Email."""

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        connection_string = settings.ACS_EMAIL_CONNECTION_STRING
        sender = settings.ACS_EMAIL_SENDER_ADDRESS
        if not connection_string or not sender:
            if not self.fail_silently:
                raise ValueError(
                    'ACS_EMAIL_CONNECTION_STRING / ACS_EMAIL_SENDER_ADDRESS nie są skonfigurowane.'
                )
            return 0

        from azure.communication.email import EmailClient
        from azure.core.exceptions import HttpResponseError

        client = EmailClient.from_connection_string(connection_string)
        sent_count = 0
        for message in email_messages:
            to = list(message.to)
            if not to:
                continue

            if _rate_limited():
                logger.warning(
                    'Lokalny limit wysyłek (środowisko testowe - darmowa domena Azure) '
                    'osiągnięty, pomijam wysyłkę bez odpytywania Azure.'
                )
                if not self.fail_silently:
                    raise RuntimeError(
                        'Osiągnięto lokalny limit wysyłek dla testowej domeny Azure '
                        '(kilka maili na minutę / kilkanaście na godzinę). Spróbuj za chwilę.'
                    )
                continue

            payload = {
                'senderAddress': sender,
                'recipients': {
                    'to': [{'address': addr} for addr in to],
                    'cc': [{'address': addr} for addr in (message.cc or [])],
                    'bcc': [{'address': addr} for addr in (message.bcc or [])],
                },
                'content': {
                    'subject': message.subject,
                    'plainText': message.body,
                },
            }
            attachments = _build_attachments(message)
            if attachments:
                payload['attachments'] = attachments

            try:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    _record_send_attempt()
                    try:
                        poller = client.begin_send(payload)
                        poller.result()
                        sent_count += 1
                        break
                    except HttpResponseError as e:
                        if e.status_code == 429 and attempt < MAX_ATTEMPTS:
                            logger.warning(
                                'Azure Communication Services – limit żądań (próba %s/%s), ponawiam za %ss',
                                attempt, MAX_ATTEMPTS, RETRY_BACKOFF_SECONDS,
                            )
                            time.sleep(RETRY_BACKOFF_SECONDS)
                            continue
                        raise
            except Exception:
                logger.exception('Błąd wysyłki maila przez Azure Communication Services')
                if not self.fail_silently:
                    raise
        return sent_count

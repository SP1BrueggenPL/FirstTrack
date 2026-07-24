import logging
import time

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2


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
            try:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        poller = client.begin_send(payload)
                        poller.result()
                        sent_count += 1
                        break
                    except HttpResponseError as e:
                        if e.status_code == 429 and attempt < MAX_ATTEMPTS:
                            logger.warning(
                                'Azure Communication Services – limit żądań (próba %s/%s), ponawiam za %ss',
                                attempt, MAX_ATTEMPTS, RETRY_BACKOFF_SECONDS * attempt,
                            )
                            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                            continue
                        raise
            except Exception:
                logger.exception('Błąd wysyłki maila przez Azure Communication Services')
                if not self.fail_silently:
                    raise
        return sent_count

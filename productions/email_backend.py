import logging

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


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
                poller = client.begin_send(payload)
                poller.result()
                sent_count += 1
            except Exception:
                logger.exception('Błąd wysyłki maila przez Azure Communication Services')
                if not self.fail_silently:
                    raise
        return sent_count

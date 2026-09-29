"""E-mail adapters (F18): Resend over HTTPS and SMTP with STARTTLS.

Implements: F18, C3, C4, 6.2

Both adapters send one plain-text message with a Reply-To. Errors are mapped to
``EmailResult`` with a retryable flag so the e-mail outbox can back off. Credentials
come from the environment only (Appendix A) and never appear in results or logs.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid

import httpx2

from concierge.notify.port import EmailMessage, EmailPort, EmailResult
from concierge.settings import Settings

RESEND_URL = "https://api.resend.com/emails"  # VERIFY (Resend API v1, region VERIFY)


class ResendEmail:
    name = "resend"

    def __init__(
        self,
        api_key: str,
        *,
        url: str = RESEND_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self.url = url
        self.transport = transport
        self.timeout_s = timeout_s

    async def send(self, message: EmailMessage, *, sender: str) -> EmailResult:
        body: dict[str, object] = {
            "from": sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.reply_to:
            body["reply_to"] = message.reply_to
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if message.idempotency_key:
            headers["Idempotency-Key"] = message.idempotency_key[:256]
        kwargs: dict[str, object] = {"timeout": self.timeout_s}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            async with httpx2.AsyncClient(**kwargs) as client:  # type: ignore[arg-type]
                response = await client.post(self.url, json=body, headers=headers)
        except httpx2.HTTPError as exc:
            return EmailResult(ok=False, error=f"transport: {type(exc).__name__}", retryable=True)
        if response.status_code in {200, 201}:
            try:
                provider_id = str(response.json().get("id", ""))
            except ValueError:
                provider_id = ""
            return EmailResult(ok=True, provider_id=provider_id)
        retryable = response.status_code == 429 or response.status_code >= 500
        return EmailResult(ok=False, error=f"http {response.status_code}", retryable=retryable)


class SmtpEmail:
    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int,
        *,
        user: str = "",
        password: str = "",
        starttls: bool = True,
        timeout_s: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self._user = user
        self._password = password
        self.starttls = starttls
        self.timeout_s = timeout_s

    def _send_sync(self, message: EmailMessage, sender: str) -> EmailResult:
        mime = MimeMessage()
        mime["From"] = sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=False)
        mime["Message-ID"] = make_msgid()
        if message.reply_to:
            mime["Reply-To"] = message.reply_to
        mime.set_content(message.text, charset="utf-8")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as smtp:
                if self.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self._user:
                    smtp.login(self._user, self._password)
                smtp.send_message(mime)
        except smtplib.SMTPRecipientsRefused as exc:
            return EmailResult(ok=False, error=f"recipient refused: {type(exc).__name__}")
        except smtplib.SMTPAuthenticationError as exc:
            return EmailResult(ok=False, error=f"authentication: {type(exc).__name__}")
        except (smtplib.SMTPException, OSError) as exc:
            return EmailResult(ok=False, error=f"smtp: {type(exc).__name__}", retryable=True)
        return EmailResult(ok=True, provider_id=str(mime["Message-ID"]))

    async def send(self, message: EmailMessage, *, sender: str) -> EmailResult:
        return await asyncio.to_thread(self._send_sync, message, sender)


def build_email_port(settings: Settings) -> EmailPort | None:
    """The configured adapter, or None when e-mail is off or not configured."""
    if settings.gc_email_provider == "resend":
        key = settings.resend_api_key.get_secret_value()
        return ResendEmail(key) if key else None
    if settings.gc_email_provider == "smtp":
        if not settings.gc_smtp_host:
            return None
        return SmtpEmail(
            settings.gc_smtp_host,
            settings.gc_smtp_port,
            user=settings.gc_smtp_user,
            password=settings.gc_smtp_password.get_secret_value(),
            starttls=settings.gc_smtp_starttls,
        )
    return None

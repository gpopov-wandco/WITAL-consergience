"""E-mail adapters (F18): Resend over a mock transport, SMTP over a fake client, the
provider selection from the settings."""

from __future__ import annotations

import smtplib
from typing import Any

import httpx2
import pytest
from tests.unit.test_doctor import with_

from concierge.notify.email import ResendEmail, SmtpEmail, build_email_port
from concierge.notify.port import EmailMessage
from concierge.settings import Settings

pytestmark = pytest.mark.feature("F18")

MESSAGE = EmailMessage(
    to="guest@example.invalid",
    subject="Ihre Anfrage",
    text="Guten Tag",
    reply_to="reception@example.invalid",
    idempotency_key="inq-1",
    html="<p>Guten Tag</p>",
)


async def test_resend_maps_responses_and_transport_errors() -> None:
    seen: list[httpx2.Request] = []
    status = {"code": 201}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if status["code"] == -1:
            raise httpx2.ConnectError("down")
        body = {"id": "em_1"} if status["code"] in {200, 201} else {"message": "no"}
        return httpx2.Response(status["code"], json=body)

    adapter = ResendEmail("re_fake_key_for_tests", transport=httpx2.MockTransport(handler))
    ok = await adapter.send(MESSAGE, sender="Hotel <mail@example.invalid>")
    assert ok.ok and ok.provider_id == "em_1"
    request = seen[0]
    assert request.headers["authorization"] == "Bearer re_fake_key_for_tests"
    assert request.headers["idempotency-key"] == "inq-1"
    payload = request.read()
    assert b'"reply_to":"reception@example.invalid"' in payload
    assert b'"to":["guest@example.invalid"]' in payload and b"Guten Tag" in payload
    for code, retryable in ((429, True), (503, True), (422, False)):
        status["code"] = code
        result = await adapter.send(MESSAGE, sender="s")
        assert not result.ok and result.error == f"http {code}" and result.retryable is retryable
    status["code"] = -1
    down = await adapter.send(MESSAGE, sender="s")
    assert not down.ok and down.error == "transport: ConnectError" and down.retryable
    assert "re_fake_key" not in down.error


class FakeSmtp:
    calls: list[tuple[str, Any]] = []
    failure: BaseException | None = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        FakeSmtp.calls.append(("connect", (host, port, timeout)))

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        FakeSmtp.calls.append(("starttls", context is not None))

    def login(self, user: str, password: str) -> None:
        FakeSmtp.calls.append(("login", user))

    def send_message(self, mime: Any) -> None:
        if FakeSmtp.failure is not None:
            raise FakeSmtp.failure
        FakeSmtp.calls.append(("send", (mime["To"], mime["Reply-To"], mime.get_content_type())))


async def test_smtp_sends_with_starttls_and_maps_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    FakeSmtp.calls.clear()
    FakeSmtp.failure = None
    adapter = SmtpEmail("mail.example.invalid", 587, user="u", password="p", starttls=True)
    result = await adapter.send(MESSAGE, sender="Hotel <mail@example.invalid>")
    assert result.ok and result.provider_id.startswith("<")
    kinds = [c[0] for c in FakeSmtp.calls]
    assert kinds == ["connect", "starttls", "login", "send"]
    assert FakeSmtp.calls[-1][1][0] == "guest@example.invalid"
    assert FakeSmtp.calls[-1][1][1] == "reception@example.invalid"
    assert FakeSmtp.calls[-1][1][2] == "multipart/alternative"  # text plus html
    for failure, fragment, retryable in (
        (smtplib.SMTPRecipientsRefused({}), "recipient refused", False),
        (smtplib.SMTPAuthenticationError(535, b"no"), "authentication", False),
        (smtplib.SMTPServerDisconnected("gone"), "smtp: SMTPServerDisconnected", True),
        (OSError("refused"), "smtp: OSError", True),
    ):
        FakeSmtp.failure = failure
        result = await adapter.send(MESSAGE, sender="s")
        assert not result.ok and fragment in result.error and result.retryable is retryable
    FakeSmtp.failure = None
    FakeSmtp.calls.clear()
    plain = SmtpEmail("mail.example.invalid", 25, starttls=False)
    assert (await plain.send(MESSAGE, sender="s")).ok
    assert [c[0] for c in FakeSmtp.calls] == ["connect", "send"]  # no TLS, no login


def test_provider_selection_from_the_settings(settings: Settings) -> None:
    assert build_email_port(settings) is None  # no provider configured
    assert build_email_port(with_(settings, gc_email_provider="resend")) is None
    resend = build_email_port(
        with_(settings, gc_email_provider="resend", resend_api_key="re_fake_key_for_tests")
    )
    assert isinstance(resend, ResendEmail)
    assert build_email_port(with_(settings, gc_email_provider="smtp")) is None
    smtp = build_email_port(
        with_(
            settings,
            gc_email_provider="smtp",
            gc_smtp_host="mail.example.invalid",
            gc_smtp_port=2525,
            gc_smtp_user="u",
            gc_smtp_password="p",
            gc_smtp_starttls=False,
        )
    )
    assert isinstance(smtp, SmtpEmail) and smtp.port == 2525 and smtp.starttls is False

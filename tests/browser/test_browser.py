"""Browser suite (10.1, marker ``browser``): the widget and the admin driven with Playwright
against a demo server started from the CLI.

Run with ``pytest -m browser tests/browser``; the default run deselects the marker. The
server runs in a subprocess with a fresh home directory; Chromium comes from
``PLAYWRIGHT_BROWSERS_PATH``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect, sync_playwright

pytestmark = pytest.mark.browser

OWNER = "owner@hotel-beispiel.invalid"
OWNER_PASSWORD = "demo-owner-password"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """``concierge setup`` then ``concierge demo`` in a subprocess; yields the base URL."""
    home = tmp_path_factory.mktemp("home")
    work = tmp_path_factory.mktemp("work")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GC_", "ANTHROPIC", "RESEND"))}
    env["HOME"] = str(home)
    env["PYTHONUNBUFFERED"] = "1"
    executable = Path(sys.executable).with_name("concierge")
    subprocess.run(
        [str(executable), "setup", "--non-interactive"],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )
    port = _free_port()
    log = (work / "demo.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(executable), "demo", "--port", str(port)],
        cwd=work,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(90):
            try:
                with urllib.request.urlopen(f"{base}/readyz", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("demo server did not become ready")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


def _chromium_executable() -> str | None:
    """A pre-installed Chromium (``GC_BROWSER_EXECUTABLE`` or the ``chromium`` link in
    ``PLAYWRIGHT_BROWSERS_PATH``); ``None`` lets Playwright use its own download."""
    explicit = os.environ.get("GC_BROWSER_EXECUTABLE")
    if explicit and Path(explicit).exists():
        return explicit
    linked = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")) / "chromium"
    return str(linked) if linked.exists() else None


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(executable_path=_chromium_executable())
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 390, "height": 844}, locale="de-DE")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    yield page
    context.close()
    assert errors == [], errors


@pytest.mark.feature("F22")
def test_widget_is_inert_then_keyboard_operable_and_answers(page: Page, demo_server: str) -> None:
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(f"{demo_server}/demo/widget")
    launcher = page.locator("#gc-widget-host button")
    expect(launcher).to_have_text("Chat")
    # the host page's own htmx history key is not the widget's; the widget stores nothing yet
    stored = page.evaluate("() => [...Object.keys(sessionStorage), ...Object.keys(localStorage)]")
    assert [key for key in stored if not key.startswith("htmx-")] == [], stored
    assert page.context.cookies() == []
    assert not any("/w/api/" in url or "gc-widget.js" in url for url in requests)  # inert

    launcher.focus()
    page.keyboard.press("Enter")
    panel = page.locator("#gc-widget-host section.gc")
    expect(panel).to_be_visible()
    expect(page.locator("#gc-widget-host .gc-ai")).to_have_text("KI-Assistent")
    assert (
        page.evaluate("() => document.getElementById('gc-widget-host').shadowRoot.activeElement.id")
        == "gc-input"
    )
    expect(page.locator("#gc-widget-host .gc-msg.bot").first).to_contain_text("KI-Assistent")

    page.keyboard.type("Gibt es einen Parkplatz?")
    page.keyboard.press("Enter")
    expect(page.locator("#gc-widget-host .gc-msg.user")).to_have_text("Gibt es einen Parkplatz?")
    answer = page.locator("#gc-widget-host .gc-msg.bot", has_text="18 EUR")
    expect(answer).to_be_visible(timeout=20_000)
    assert (
        page.evaluate(
            "() => document.querySelector('#gc-widget-host').shadowRoot.querySelector('.gc-log').getAttribute('aria-live')"
        )
        == "polite"
    )
    session_keys = page.evaluate(
        "() => Object.keys(sessionStorage).filter((k) => k.startsWith('gc_'))"
    )
    assert len(session_keys) == 1 and session_keys[0].startswith("gc_session_")  # session id only
    assert page.evaluate("() => Object.keys(localStorage).filter((k) => k.startsWith('gc_'))") == []
    assert page.context.cookies() == []

    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    assert page.evaluate(
        "() => document.activeElement === document.querySelector('#gc-widget-host button')"
    )
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth"
    )  # no horizontal scroll at 390 px


@pytest.mark.feature("F19")
def test_admin_login_inbox_and_questionnaire(page: Page, demo_server: str) -> None:
    page.goto(f"{demo_server}/admin/login")
    page.fill("input[name=email]", OWNER)
    page.fill("input[name=password]", OWNER_PASSWORD)
    page.keyboard.press("Enter")
    page.wait_for_url(f"{demo_server}/admin/")
    expect(page.locator("h1")).to_be_visible()
    page.goto(f"{demo_server}/admin/inbox")
    expect(page.locator("h1")).to_contain_text("Posteingang")
    page.goto(f"{demo_server}/admin/questionnaire")
    expect(page.locator("h1")).to_have_text("Fragebogen")
    fields = page.locator("form[action='/admin/questionnaire'] [name]:not([name=csrf])")
    assert fields.count() == 85
    page.fill("textarea[name=a_hotel_name]", "Hotel Beispiel")
    page.fill("input[name=d_checkin_from]", "15:00")
    page.locator("form[action='/admin/questionnaire'] button[type=submit]").click()
    page.wait_for_url(f"{demo_server}/admin/questionnaire?notice=saved")
    expect(page.locator("p.notice")).to_have_text("Gespeichert.")
    expect(page.locator("input[name=d_checkin_from]")).to_have_value("15:00")
    assert page.locator("script:not([src])").count() == 0  # no inline scripts in the admin

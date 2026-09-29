"""The PDF handout (section 13, F34): how to test, hand over and customise.

Implements: F34, 13

ReportLab only. A4, at most six pages, English. Brand: a dark header band with the
"WITAL" (white) "CO" (red) lockup, Syne headings, Inter body. Every command shown is
listed in ``HANDOUT_COMMANDS`` so the verification run executes each one literally.
Wording follows the register of the specification: concise, neutral, no claims that
cannot be checked, no exclamation marks.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from concierge import __version__
from concierge.reports.pdf import FONTS_DIR

INK = colors.HexColor("#141414")
PANEL = colors.HexColor("#0F1012")
RED = colors.HexColor("#C22113")
PAGE_BG = colors.HexColor("#F2F2F2")
MUTED = colors.HexColor("#5b5b5b")
LINE = colors.HexColor("#d9d9d9")
MAX_PAGES = 6
_fonts_ready = False

LINUX_INSTALL = [
    "python3.13 -m venv .venv",
    ". .venv/bin/activate",
    "python -m pip install --require-hashes -r requirements.lock",
    "python -m pip install --no-deps --no-build-isolation -e .",
    "concierge setup --non-interactive",
    "concierge demo",
]
WINDOWS_INSTALL = [
    "py -3.13 -m venv .venv",
    r".\.venv\Scripts\Activate.ps1",
    "python -m pip install --require-hashes -r requirements.lock",
    "python -m pip install --no-deps --no-build-isolation -e .",
    "concierge setup --non-interactive",
    "concierge demo",
]
SCENARIOS = [
    (
        "1 Question, German",
        'concierge wa-sim send --tenant hotel-beispiel --text "Gibt es einen Parkplatz?"',
        "a German answer with the garage price from the knowledge base; no space is promised",
    ),
    (
        "2 Question, English",
        'concierge wa-sim send --tenant hotel-beispiel --text "When is breakfast served?" --from 4915120000002',
        "an English answer with the breakfast hours and price",
    ),
    (
        "3 Availability and booking link",
        'concierge wa-sim send --tenant hotel-beispiel --text "Haben Sie vom 06.10.2026 bis 08.10.2026 ein Zimmer für 2 Erwachsene?"',
        "a quote from the manual calendar with its date, the note that it is not binding, and the booking link",
    ),
    (
        "4 Late arrival",
        'concierge wa-sim send --tenant hotel-beispiel --text "Wir kommen erst spät nach 22 Uhr an"',
        "the key-safe procedure; the code itself is sent on the arrival day to verified guests only",
    ),
    (
        "5 Complaint reaching the inbox",
        'concierge wa-sim send --tenant hotel-beispiel --text "Die Dusche ist kaputt und das Zimmer ist dreckig"',
        "an acknowledgement, a P1 handoff in the inbox (/admin/inbox) and a staff alert",
    ),
]
ACCEPTANCE = [
    "Install from the lock file completes without errors",
    "concierge setup prints the one-time admin link",
    "concierge demo answers /readyz with 200",
    "Scenario 1 answers in German from the knowledge base",
    "Scenario 2 answers in English",
    "Scenario 3 quotes the calendar with its date and the booking link",
    "Scenario 4 withholds the key-safe code before the arrival day",
    "Scenario 5 creates a P1 handoff in the inbox",
    "A staff reply from the inbox reaches the simulator page",
    "STOP ends automated messages and confirms once",
    "An emergency message answers at once with 112 and 110",
    "pytest ends with every test passed",
    "concierge doctor shows no failure",
    "concierge doctor --live names the variable for every missing key",
    "The widget test page opens, sends and receives with the keyboard only",
]
LIVE_SMOKE = [
    "Create a Meta test WhatsApp Business Account with its test number and add your phone as a recipient.",
    "Enter the credentials during a call: concierge secrets set hotel-beispiel wa_access_token, wa_app_secret and wa_verify_token (hidden prompts); set whatsapp.waba_id and whatsapp.phone_number_id in the tenant profile.",
    "Expose the service: a public HTTPS host or a tunnel to port 8000; set GC_PUBLIC_BASE_URL and register the public URL followed by /wa/webhook with the verify token, fields messages and message_template_status_update.",
    "Send the hello_world template from the Meta console to your phone, reply from your phone, and read the answer.",
    "Check the inbox (/admin/inbox) and the usage report: concierge usage export --tenant hotel-beispiel --month 2026-10",
]
TRAINING = [
    "Inbox: filters, SLA countdown, translated view, reply, template picker, resolve (15 min)",
    "Knowledge base: drafts, approval, review dates, the gap report (10 min)",
    "Escalation: priorities, staffed hours, on-call number, the kill switch (5 min)",
    "Templates and journeys: statuses, preview, test send, quiet hours (10 min)",
    "Reports and data requests: digest, usage export, export and erasure (5 min)",
]
ACCEPTANCE_PROTOCOL = [
    "The hotel confirms the approved knowledge base entries and legal texts by name and date.",
    "The five scenarios and the live smoke test are run together and the results recorded.",
    "concierge doctor --live shows no failure for the tenant; concierge tenant activate succeeds.",
    "The hotel names the escalation contacts, staffed hours and the on-call number.",
    "Both sides sign the processor agreement and the sub-processor acknowledgement.",
]
OFFBOARDING = [
    "concierge pack export <slug> --out <dir> hands the hotel its configuration without secrets.",
    "concierge tenant pause <slug> stops answers and journeys; the hotel revokes the system-user token in its Meta settings.",
    "concierge gdpr export or erase handles individual requests; concierge secrets delete removes stored credentials; retention removes the rest on schedule.",
]
SUPPORT = [
    (
        "WITALCO supports",
        "the service, updates, backups, monitoring, key rotation, incident handling, template registration, the doctor",
    ),
    (
        "The hotel keeps current",
        "facts and prices in the knowledge base, availability in the calendar or PMS, template approvals, staffed hours and contacts, the legal texts after lawyer review",
    ),
    (
        "Neither side",
        "confirms bookings, prices or refunds through the assistant; staff decide, the assistant records",
    ),
]
PMS_CHOICE = [
    ("Beds24", "invite code, refresh token; read scopes; 24-hour tokens; shared credit budget"),
    ("apaleo", "client credentials; offers with fees and city tax; read-only"),
    ("Mews", "Distributor ids for reads; Connector after certification"),
    ("iCal", "one feed URL per unit; counts only, no prices"),
    (
        "manual",
        "the calendar grid in the admin or a CSV; data older than 36 hours is quoted as of its date",
    ),
    ("none", "the reception is asked; the assistant creates a handoff"),
]
GO_LIVE = [
    "legal texts approved with name and date",
    "processor agreement signed and sub-processors acknowledged (concierge tenant record-dpa)",
    "core knowledge base keys approved in every tenant language",
    "WhatsApp number mapped uniquely; templates approved in every language",
    "concierge doctor --live recorded a passing run",
    "concierge tenant activate <slug>",
]
TROUBLESHOOTING = [
    (
        "No answer to a message",
        "worker heartbeat, token state, dead jobs",
        "curl /readyz; concierge doctor; operator console",
    ),
    (
        "Signature rejected on the webhook",
        "wa_app_secret matches the app",
        "concierge secrets set <slug> wa_app_secret",
    ),
    (
        "Template refused when sending",
        "status APPROVED in that language",
        "concierge whatsapp templates list <slug>",
    ),
    (
        "Wrong fact in an answer",
        "the approved KB entry",
        "admin: flag the reply, fix in KB, approve",
    ),
    (
        "No live availability",
        "chain, credentials, feed age",
        "concierge doctor --live --tenant <slug>",
    ),
    (
        "Model unavailable",
        "spend cap, circuit state; the responder answers meanwhile",
        "operator console; ANTHROPIC_API_KEY",
    ),
    ("Backup missing", "the backup directory and job", "concierge backup; concierge restore-test"),
    (
        "Disk full",
        "free space in the data directory",
        "concierge backup --keep-days 14; concierge doctor",
    ),
]
WHERE = [
    ("Service", "one process, port 8000; behind Caddy on 443"),
    ("Environment", ".env in the working directory or /etc/gc/env (mode 0600)"),
    (
        "Data",
        "~/.witalco-concierge/concierge.db (SQLite) or GC_DATABASE_URL (PostgreSQL); backups in the backups folder next to it",
    ),
    ("Logs", "stdout as JSON; journalctl -u concierge; no guest text in logs"),
    ("Admin", "/admin/login; operator console /operator/; metrics /metrics with GC_METRICS_TOKEN"),
    ("Demo", "/demo/widget, /demo/wa-sim; fixture accounts printed by concierge demo"),
    ("Docs", "docs/OPERATIONS.md, INTEGRATIONS.md, CUSTOMISATION.md, COMPLIANCE.md, SECURITY.md"),
]
HANDOUT_COMMANDS: list[str] = [
    *LINUX_INSTALL[:1],
    "concierge setup --non-interactive",
    "concierge demo",
    *[command for _, command, _ in SCENARIOS],
    "pytest",
    "concierge doctor",
    "concierge doctor --live --tenant hotel-beispiel",
    "concierge usage export --tenant hotel-beispiel --month 2026-10",
    "concierge secrets set hotel-beispiel wa_access_token",
    "concierge backup",
    "concierge restore-test",
    "concierge compliance pack --tenant hotel-beispiel",
    "concierge pack export hotel-beispiel --out packs/hotel-beispiel",
    "concierge pack validate packs/hotel-beispiel",
    "concierge pack diff packs/hotel-beispiel",
    "concierge pack import packs/hotel-beispiel",
    "concierge tenant pause hotel-beispiel",
    "concierge tenant unpause hotel-beispiel",
    "concierge tenant record-dpa hotel-beispiel",
    "concierge whatsapp templates lint",
    "concierge whatsapp templates list hotel-beispiel",
    "concierge tenant activate hotel-beispiel",
    "concierge whatsapp templates sync hotel-beispiel",
]


@dataclass
class Styles:
    body: ParagraphStyle
    small: ParagraphStyle
    h1: ParagraphStyle
    h2: ParagraphStyle
    code: ParagraphStyle
    cell: ParagraphStyle


def register_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    pdfmetrics.registerFont(TTFont("Inter", str(FONTS_DIR / "Inter-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Inter-Bold", str(FONTS_DIR / "Inter-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("Syne-Bold", str(FONTS_DIR / "Syne-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("Syne", str(FONTS_DIR / "Syne-Regular.ttf")))
    _fonts_ready = True


def styles() -> Styles:
    body = ParagraphStyle(
        "body", fontName="Inter", fontSize=8.6, leading=11.2, textColor=INK, alignment=TA_LEFT
    )
    return Styles(
        body=body,
        small=ParagraphStyle("small", parent=body, fontSize=7.6, leading=9.6, textColor=MUTED),
        h1=ParagraphStyle(
            "h1",
            fontName="Syne-Bold",
            fontSize=15,
            leading=18,
            textColor=INK,
            spaceBefore=4,
            spaceAfter=4,
        ),
        h2=ParagraphStyle(
            "h2",
            fontName="Syne-Bold",
            fontSize=10.5,
            leading=13,
            textColor=RED,
            spaceBefore=5,
            spaceAfter=2,
        ),
        code=ParagraphStyle(
            "code",
            parent=body,
            fontName="Courier",
            fontSize=7.8,
            leading=10,
            backColor=PAGE_BG,
            leftIndent=4,
            borderPadding=(1, 3, 1, 3),
        ),
        cell=ParagraphStyle("cell", parent=body, fontSize=7.8, leading=9.8),
    )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_esc(text), style)


def _code(lines: Sequence[str], st: Styles) -> list[Any]:
    return [Paragraph(_esc(line), st.code) for line in lines]


def _table(
    rows: Sequence[Sequence[str]], st: Styles, widths: Sequence[float], header: bool = True
) -> Table:
    data = [[Paragraph(_esc(c), st.cell) for c in row] for row in rows]
    table = Table(data, colWidths=list(widths), repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), PAGE_BG),
            ("FONTNAME", (0, 0), (-1, 0), "Inter-Bold"),
        ]
    table.setStyle(TableStyle(style))
    return table


def _bullets(items: Sequence[str], st: Styles) -> list[Any]:
    return [Paragraph("• " + _esc(item), st.body) for item in items]


def _page_frame(canvas: Any, doc: Any) -> None:
    width, height = A4
    canvas.saveState()
    canvas.setFillColor(PANEL)
    canvas.rect(0, height - 16 * mm, width, 16 * mm, stroke=0, fill=1)
    canvas.setFont("Syne-Bold", 15)
    canvas.setFillColor(colors.white)
    canvas.drawString(14 * mm, height - 10.5 * mm, "WITAL")
    wital = pdfmetrics.stringWidth("WITAL", "Syne-Bold", 15)
    canvas.setFillColor(RED)
    canvas.drawString(14 * mm + wital, height - 10.5 * mm, "CO")
    canvas.setFont("Inter", 8.5)
    canvas.setFillColor(colors.white)
    canvas.drawRightString(width - 14 * mm, height - 10.5 * mm, "Guest Concierge handout")
    canvas.setFont("Inter", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(
        14 * mm,
        8 * mm,
        f"WITALCO GmbH · Guest Concierge {__version__} · {doc.handout_date.isoformat()}",
    )
    canvas.drawRightString(width - 14 * mm, 8 * mm, f"page {doc.page} of at most {MAX_PAGES}")
    canvas.restoreState()


def build_handout(out: Path, *, handout_date: date | None = None) -> Path:
    """Write the handout PDF and return its path."""
    register_fonts()
    st = styles()
    when = handout_date or datetime.now(UTC).date()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=22 * mm,
        bottomMargin=14 * mm,
        title="WITALCO Guest Concierge handout",
        author="WITALCO GmbH",
        subject="How to test, hand over and customise the Guest Concierge",
    )
    doc.handout_date = when
    usable = A4[0] - 28 * mm
    story: list[Any] = []

    # ---------------------------------------------------------------- Part A
    story.append(_p("Part A. How to test", st.h1))
    story.append(
        _p(
            "The service runs in demo mode on one machine without API keys: the fictional Hotel Beispiel is seeded, a mock WhatsApp server records sends and the scripted responder answers instead of the language model. Python 3.13 and git are required.",
            st.body,
        )
    )
    story.append(_p("Install and start the demo", st.h2))
    story.append(_p("Linux or macOS, in the repository directory:", st.body))
    story.extend(_code(LINUX_INSTALL, st))
    story.append(_p("Windows, PowerShell:", st.body))
    story.extend(_code(WINDOWS_INSTALL, st))
    story.append(
        _p(
            "concierge setup writes .env with generated keys and prints a one-time admin link. concierge demo starts the service on http://127.0.0.1:8000 and prints the widget test page, the simulator page and the fixture accounts. Open /readyz to confirm the worker and the database.",
            st.body,
        )
    )
    story.append(_p("Five guided scenarios", st.h2))
    story.append(
        _p(
            "Run each command in a second terminal with the environment activated while the demo is running; read the answers on /demo/wa-sim?from=4915112345678 (scenario 2 on the page of its own number) and in the inbox.",
            st.body,
        )
    )
    story.append(
        _table(
            [("Scenario", "Command", "Expected")] + [(a, b, c) for a, b, c in SCENARIOS],
            st,
            [usable * 0.16, usable * 0.5, usable * 0.34],
        )
    )
    story.append(_p("The test suite", st.h2))
    story.append(
        _p(
            "pytest runs the offline suite (unit, integration, golden cases in six languages, property, end-to-end) with no network and no keys; the run ends with a line stating that every test passed. concierge check prints the static gates in one table. pytest -m load tests/load -s prints the load smoke numbers.",
            st.body,
        )
    )
    story.extend(_code(["pytest", "concierge check"], st))
    story.append(_p("The doctor", st.h2))
    story.append(
        _p(
            "concierge doctor checks the configuration offline and ends with pass, warn and fail counts (exit codes 0, 1, 2); in the demo the live integrations report not configured. With keys entered, concierge doctor --live --tenant hotel-beispiel probes every integration and names the variable or field to fix for each failure; the result is recorded and read by the activation gate.",
            st.body,
        )
    )
    story.extend(_code(["concierge doctor", "concierge doctor --live --tenant hotel-beispiel"], st))
    story.append(_p("Live smoke test with a WhatsApp test number", st.h2))
    story.extend(_bullets(LIVE_SMOKE, st))
    story.append(_p("Acceptance checklist", st.h2))
    rows = [
        ("☐ " + a, "☐ " + b)
        for a, b in zip(ACCEPTANCE[0::2], [*ACCEPTANCE[1::2], ""], strict=False)
    ]
    story.append(_table(rows, st, [usable / 2, usable / 2], header=False))
    story.append(Spacer(1, 6))

    # ---------------------------------------------------------------- Part B
    story.append(_p("Part B. How to hand it over", st.h1))
    story.append(_p("Credentials without secrets in files", st.h2))
    story.append(
        _p(
            "The hotel creates the system-user token in its own Meta business settings and keeps ownership of the WhatsApp account. Tokens, app secret and PMS credentials are entered during a call with concierge secrets set <tenant> <name> (hidden prompt) or the write-only field in the admin; they are never sent by e-mail or chat and never written into a pack. Platform keys stay in the environment file of the server (mode 0600).",
            st.body,
        )
    )
    story.extend(_code(["concierge secrets set hotel-beispiel wa_access_token"], st))
    story.append(_p("Roles", st.h2))
    story.append(
        _p(
            "Owner: everything in the hotel admin, including approvals and team invites. Staff: inbox, conversations, availability, reservations. Operator (WITALCO): the cross-tenant console, kill switch, prices, activation; every tenant switch is audited. Invites are single-use links valid for fifteen minutes; there are no default passwords.",
            st.body,
        )
    )
    story.append(_p("Backups and the restore test", st.h2))
    story.append(
        _p(
            "concierge backup writes an encrypted file per run and prunes old ones; run it daily and copy the directory off the machine. concierge restore-test restores the newest file into a scratch database and compares the migration head and row counts. Backups contain guest data and follow the same retention.",
            st.body,
        )
    )
    story.extend(_code(["concierge backup", "concierge restore-test"], st))
    story.append(_p("Monitoring and alerts", st.h2))
    story.append(
        _p(
            "/healthz and /readyz for the process, /metrics for Prometheus, operator alerts by e-mail for invalid tokens, the model spend cap, an open circuit, dead jobs, paused templates, failed backups and stale price tables. Staff alerts go to the configured WhatsApp numbers and e-mail addresses per priority.",
            st.body,
        )
    )
    story.append(_p("Processor agreement and sub-processors", st.h2))
    story.append(
        _p(
            "concierge compliance pack --tenant <slug> writes the sub-processor list with locations, the retention schedule, the TOM checklist, the AI disclosures per language and a record-of-processing draft, each page stamped Draft for lawyer review. Anthropic and Retell process data in the United States under standard contractual clauses; a hotel that declines this runs with ai_mode faq_only and without the voice line.",
            st.body,
        )
    )
    story.extend(_code(["concierge compliance pack --tenant hotel-beispiel"], st))
    story.append(_p("Training agenda (45 minutes)", st.h2))
    story.extend(_bullets(TRAINING, st))
    story.append(_p("Acceptance protocol", st.h2))
    story.extend(_bullets(ACCEPTANCE_PROTOCOL, st))
    story.append(_p("Offboarding and data deletion", st.h2))
    story.extend(_bullets(OFFBOARDING, st))
    story.append(_p("Support boundaries", st.h2))
    story.append(
        _table([(a, b) for a, b in SUPPORT], st, [usable * 0.22, usable * 0.78], header=False)
    )
    story.append(Spacer(1, 6))

    # ---------------------------------------------------------------- Part C
    story.append(_p("Part C. How to customise per client", st.h1))
    story.append(_p("Client pack workflow", st.h2))
    story.append(
        _p(
            "A pack is a directory or zip with tenant.yaml, kb/, templates/, legal/, journeys.yaml, availability.yaml, branding/ and questionnaire_answers.yaml; it never holds secrets. Export the fixture pack, edit it, validate, compare and import; the import is a dry run first, writes drafts and one audit row.",
            st.body,
        )
    )
    story.extend(
        _code(
            [
                "concierge pack export hotel-beispiel --out packs/hotel-beispiel",
                "concierge pack validate packs/hotel-beispiel",
                "concierge pack diff packs/hotel-beispiel",
                "concierge pack import packs/hotel-beispiel",
            ],
            st,
        )
    )
    story.append(_p("The questionnaire and who fills it", st.h2))
    story.append(
        _p(
            "Eighty-five items in nine groups: business and contacts, channels, PMS and availability, times and arrival, policies and money, facilities, guest information, handoff and staff, brand and legal. The hotel owner or manager answers them in the admin or in questionnaire_answers.yaml; WITALCO reviews the answers; each one becomes a draft knowledge base entry that the owner approves. City tax rates and cancellation terms are the hotel's own statements.",
            st.body,
        )
    )
    story.append(_p("Tenant configuration, knowledge base and languages", st.h2))
    story.append(
        _p(
            "tenant.yaml holds contacts, staffed hours, handoff contacts and response windows, the availability chain and booking link, journeys and quiet hours, the widget origins and colour, limits and pricing. Only approved knowledge base entries reach guests; entries carry a review date and a sensitivity (public, verified guest only, staff only). German and English are mandatory; Russian, Ukrainian, Turkish and Italian are enabled per hotel; machine translations remain drafts until approved. Tone and refusal wording live in the staff-only entries tone and refusals; the widget takes colour, logo text and position from the profile.",
            st.body,
        )
    )
    story.append(_p("Escalation, WhatsApp number and templates", st.h2))
    story.append(
        _p(
            "Escalation contacts, staffed hours and the on-call number for P0 come from the profile. One WhatsApp number per hotel; the waba_id and phone_number_id go into the profile, the three secrets through concierge secrets set. The standard template set exists in six languages; lint checks it, sync registers missing templates at Meta and tracks their approval, and sending refuses a template that is not approved in the language.",
            st.body,
        )
    )
    story.extend(
        _code(
            [
                "concierge whatsapp templates lint",
                "concierge whatsapp templates sync hotel-beispiel",
                "concierge whatsapp templates list hotel-beispiel",
            ],
            st,
        )
    )
    story.append(_p("PMS adapter choice", st.h2))
    story.append(
        _table(
            [("Source", "What it needs and what it gives")] + [(a, b) for a, b in PMS_CHOICE],
            st,
            [usable * 0.16, usable * 0.84],
        )
    )
    story.append(_p("Go-live checklist (activation gates)", st.h2))
    story.extend(_bullets(GO_LIVE, st))
    story.extend(
        _code(
            [
                "concierge tenant record-dpa hotel-beispiel",
                "concierge tenant activate hotel-beispiel",
            ],
            st,
        )
    )
    story.append(_p("Pricing and metering", st.h2))
    story.append(
        _p(
            "Usage is metered per hotel: WhatsApp messages from Meta's status webhooks with dated price rows and the free allowance per month, model tokens in USD and EUR, voice seconds against the included minutes. The operator console edits the price tables; re-check Meta's rate card before every change; the monthly report is available as JSON, CSV and PDF. Nothing is charged or enforced by the software.",
            st.body,
        )
    )
    story.extend(_code(["concierge usage export --tenant hotel-beispiel --month 2026-10"], st))
    story.append(PageBreak())

    # ---------------------------------------------------------------- Appendix
    story.append(_p("Appendix. Troubleshooting and where things live", st.h1))
    story.append(
        KeepTogether(
            [
                _p("Troubleshooting", st.h2),
                _table(
                    [("Symptom", "Check", "Command or place")]
                    + [(a, b, c) for a, b, c in TROUBLESHOOTING],
                    st,
                    [usable * 0.28, usable * 0.32, usable * 0.4],
                ),
            ]
        )
    )
    story.append(Spacer(1, 4))
    story.append(
        KeepTogether(
            [
                _p("Where things live", st.h2),
                _table(
                    [("Item", "Location")] + [(a, b) for a, b in WHERE],
                    st,
                    [usable * 0.18, usable * 0.82],
                ),
            ]
        )
    )
    story.append(Spacer(1, 4))
    story.append(
        _p(
            "Pausing a hotel: concierge tenant pause <slug>; resuming: concierge tenant unpause <slug>. The demo accounts exist only outside production. Legal texts ship as drafts for lawyer review; the software records approvals, it does not give legal advice.",
            st.small,
        )
    )
    story.extend(
        _code(
            ["concierge tenant pause hotel-beispiel", "concierge tenant unpause hotel-beispiel"], st
        )
    )

    doc.build(story, onFirstPage=_page_frame, onLaterPages=_page_frame)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(buffer.getvalue())
    return out

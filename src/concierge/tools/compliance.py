"""The compliance pack (F34): sub-processors, retention, TOM checklist, disclosures,
record of processing; every page stamped as a draft for lawyer review.

Implements: F34, C10, C12

``concierge compliance pack --tenant X`` writes Markdown files and one PDF into a
directory. Nothing here claims compliance: the pack collects what the software does
and what a lawyer must review.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from concierge.admin.operator import SUBPROCESSORS
from concierge.agent.texts import GuestTexts, guest_texts
from concierge.reports.pdf import simple_pdf
from concierge.tenants.profile import TenantProfile

STAMP = "Draft for lawyer review"
TOM_CHECKLIST: tuple[tuple[str, str], ...] = (
    (
        "Encryption at rest",
        "tenant secrets, guest identifiers and registration data AES-256-GCM with per-tenant AAD; database disk encryption on the host is recommended (VERIFY per host)",
    ),
    (
        "Encryption in transit",
        "TLS at the proxy (Caddy); webhooks over HTTPS; vendor APIs over HTTPS",
    ),
    (
        "Access control",
        "Argon2id passwords, server sessions, lockout, single-use invites, roles owner, staff and operator, audited tenant switches",
    ),
    (
        "Pseudonymisation",
        "phones and e-mails stored encrypted with keyed blind indexes; logs carry truncated keyed hashes only",
    ),
    (
        "Separation",
        "tenant id on every business table, query-level isolation, composite unique keys",
    ),
    (
        "Integrity",
        "append-only usage, audit and deletion logs; signed webhooks verified before parsing",
    ),
    (
        "Availability",
        "encrypted daily backups with a restore test; health endpoints; job retries and dead-letter alerts",
    ),
    (
        "Deletion",
        "retention jobs per tenant with a content-free deletion log; erasure on request keeps suppression and consent proof",
    ),
    ("Logging", "structured logs without guest text or identifiers; uvicorn access log off"),
    (
        "Sub-processors",
        "listed with locations; US transfers only with standard contractual clauses and the tenant's acknowledgement",
    ),
)
PROCESSING_ACTIVITIES: tuple[tuple[str, str, str], ...] = (
    (
        "Guest conversations (WhatsApp, widget, phone)",
        "name, phone number or session id, message texts, language, handoff notes",
        "answering guest questions, escalation to staff",
    ),
    (
        "Reservations and journeys",
        "name, phone, e-mail, dates, room, opt-in evidence, ETA",
        "pre-arrival and stay messages, staff alerts",
    ),
    (
        "Registration (optional, § 29 BMG)",
        "the closed field set of § 30 Abs. 2 BMG and the payment-instrument assignment",
        "electronic registration of guests",
    ),
    ("Metering", "counts and costs per hotel, no guest content", "usage reports"),
    ("Staff accounts", "e-mail, password hash, sessions, audit trail", "operating the admin"),
)


@dataclass
class PackFile:
    name: str
    text: str


def _sub_processors(profile: TenantProfile) -> list[str]:
    lines = ["| Sub-processor | Purpose | Region | Transfer basis |", "|---|---|---|---|"]
    for row in SUBPROCESSORS:
        if row["name"].startswith("Retell") and profile.voice.provider != "retell":
            continue
        if row["name"].startswith("Stripe") and not profile.registration.enabled:
            continue
        lines.append(f"| {row['name']} | {row['purpose']} | {row['region']} | {row['transfer']} |")
    lines.append("")
    lines.append(
        "Anthropic processes model requests in the United States (`inference_geo` global or us, "
        "logged per request); a hotel that declines this runs with `ai_mode: faq_only`. "
        "Rows marked VERIFY need confirmation from the vendor's current terms."
    )
    return lines


def _retention(profile: TenantProfile) -> list[str]:
    r = profile.retention
    rows = [
        ("Chat texts", f"{r.chat_days} days after the message"),
        (
            "Special-category texts volunteered by a guest",
            "7 days after the stay, or 7 days after receipt without a booking",
        ),
        (
            "Booking-linked personal data",
            f"{r.booking_days_after_departure} days after departure (the booking skeleton stays for statistics)",
        ),
        (
            "Guest identifiers without a live booking or text",
            f"{r.chat_days} days after the last contact",
        ),
        (
            "Voice transcripts",
            f"{profile.voice.transcript_retention_days} days; no audio is recorded",
        ),
        ("Consent and suppression proof", f"{r.consent_years} years (limitation period: VERIFY)"),
        ("Usage events", f"{r.metering_months} months"),
        ("Webhook payloads", f"{r.webhook_days} days"),
        (
            "Registration data (§ 29 BMG)",
            f"{profile.registration.retention_days} days after departure, hard delete"
            if profile.registration.enabled
            else "module off",
        ),
        ("Audit and deletion logs", "kept; they carry no guest content"),
        ("Backups", "pruned by age; the same periods apply to restored copies"),
    ]
    lines = ["| Data | Retention |", "|---|---|"]
    lines.extend(f"| {a} | {b} |" for a, b in rows)
    return lines


def _disclosures(profile: TenantProfile, texts: GuestTexts) -> list[str]:
    lines = []
    for lang in profile.languages:
        chat = texts.render(
            "disclosure", lang, hotel=profile.name, privacy_url=profile.contacts.privacy_url or ""
        )
        voice = texts.render("voice_disclosure", lang, hotel=profile.name)
        lines.append(f"### {lang}")
        lines.append("")
        lines.append(f"Chat and widget: {chat}")
        lines.append("")
        lines.append(f"Phone: {voice}")
        lines.append("")
    return lines


def _record_of_processing(profile: TenantProfile) -> list[str]:
    lines = [
        f"Controller: {profile.name} (the hotel). Processor: WITALCO GmbH, Frankfurt am Main.",
        "Legal bases (to be confirmed by the lawyer): contract performance for reservation and stay communication, consent for WhatsApp journeys and marketing, legal obligation for registration (§ 29 BMG), legitimate interest for security logs.",
        "",
        "| Activity | Data | Purpose |",
        "|---|---|---|",
    ]
    lines.extend(f"| {a} | {b} | {c} |" for a, b, c in PROCESSING_ACTIVITIES)
    lines.append("")
    lines.append(
        "Recipients: the sub-processors listed above; authorities for registration data on request; nobody else."
    )
    lines.append(
        "Data-subject rights: search, export, erasure and restriction through the admin (data requests) within one month; consent withdrawal by STOP or through the hotel."
    )
    return lines


def build_pack_files(
    profile: TenantProfile,
    legal_rows: Sequence[Any],
    *,
    texts: GuestTexts | None = None,
    today: date | None = None,
) -> list[PackFile]:
    """The Markdown files of the pack; ``legal_rows`` are the tenant's legal texts."""
    texts = texts or guest_texts()
    stamp = f"{STAMP} · {profile.name}" + (f" · {today.isoformat()}" if today else "")
    files = [
        PackFile(
            "README.md",
            "\n".join(
                [
                    f"# Compliance pack: {profile.name}",
                    "",
                    stamp,
                    "",
                    "Files: subprocessors.md, retention.md, tom_checklist.md, ai_disclosures.md, record_of_processing.md, legal_texts.md, and the PDF with every section. Nothing in this pack is a statement of compliance; it lists what the software does and what a lawyer must review.",
                    "",
                ]
            ),
        ),
        PackFile(
            "subprocessors.md",
            "\n".join(["# Sub-processors", "", stamp, "", *_sub_processors(profile), ""]),
        ),
        PackFile(
            "retention.md",
            "\n".join(["# Retention schedule", "", stamp, "", *_retention(profile), ""]),
        ),
        PackFile(
            "tom_checklist.md",
            "\n".join(
                [
                    "# Technical and organisational measures",
                    "",
                    stamp,
                    "",
                    "| Measure | Implementation |",
                    "|---|---|",
                    *[f"| {a} | {b} |" for a, b in TOM_CHECKLIST],
                    "",
                ]
            ),
        ),
        PackFile(
            "ai_disclosures.md",
            "\n".join(
                ["# AI disclosures per language", "", stamp, "", *_disclosures(profile, texts)]
            ),
        ),
        PackFile(
            "record_of_processing.md",
            "\n".join(
                [
                    "# Record of processing (draft)",
                    "",
                    stamp,
                    "",
                    *_record_of_processing(profile),
                    "",
                ]
            ),
        ),
        PackFile(
            "legal_texts.md",
            "\n".join(
                [
                    "# Legal texts and their review status",
                    "",
                    stamp,
                    "",
                    "| Text | Language | Status | Approved by |",
                    "|---|---|---|---|",
                    *[
                        f"| {r.key} | {r.lang} | {r.status} | {getattr(r, 'approved_by', None) or getattr(r, 'approver', None) or ''} |"
                        for r in legal_rows
                    ],
                    "",
                    "Every text stays a draft until a named person approves it with a date in the admin (Legal).",
                    "",
                ]
            ),
        ),
    ]
    return files


def pack_pdf(
    files: Sequence[PackFile], profile: TenantProfile, *, today: date | None = None
) -> bytes:
    sections = []
    for file in files:
        if file.name == "README.md":
            continue
        lines = [
            line for line in file.text.splitlines() if line.strip() and not line.startswith("# ")
        ]
        sections.append((file.name.removesuffix(".md").replace("_", " "), lines))
    return simple_pdf(
        f"Compliance pack: {profile.name}",
        sections,
        subtitle=f"{STAMP}" + (f" · {today.isoformat()}" if today else ""),
        footer=STAMP,
    )


def write_pack(out_dir: Path, files: Sequence[PackFile], pdf: bytes, slug: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for file in files:
        path = out_dir / file.name
        path.write_text(file.text, encoding="utf-8")
        written.append(path)
    pdf_path = out_dir / f"compliance-pack-{slug}.pdf"
    pdf_path.write_bytes(pdf)
    written.append(pdf_path)
    return written

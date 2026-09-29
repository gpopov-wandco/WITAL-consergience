"""Prompt assembly (8.2): stable system blocks, cache breakpoints, context envelope.

Implements: F01, 8.2, 8.3, C6, C7

Order: tools sorted by name (the adapter sorts), system blocks, messages. System
blocks: global rules (``prompts/core.md``, versioned), tenant persona and policy, the
public KB snapshot in stable order, the channel profile. The cache breakpoint sits on
the last static block; the adapter adds the top-level breakpoint for the tail. No
date, id or guest data enters the system prompt: they travel in the envelope that
starts the newest user turn.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from concierge.clock import format_local
from concierge.llm.port import TextBlock
from concierge.tenants.profile import TenantProfile

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts"
_VERSION_RE = re.compile(r"core prompt version:\s*([0-9A-Za-z.\-]+)")


def canary_for(signing_key: bytes) -> str:
    """A process-wide marker derived from the signing key; never a code constant."""
    digest = hmac.new(signing_key, b"gc-prompt-canary", hashlib.sha256).hexdigest()[:12]
    return f"gc-canary-{digest}"


@dataclass(frozen=True)
class PromptTexts:
    core: str
    channels: dict[str, str]
    version: str

    @classmethod
    def load(cls, directory: Path = PROMPTS_DIR) -> PromptTexts:
        core = (directory / "core.md").read_text(encoding="utf-8")
        match = _VERSION_RE.search(core)
        version = match.group(1) if match else "unversioned"
        channels = {
            path.stem.removeprefix("channel_"): path.read_text(encoding="utf-8")
            for path in sorted(directory.glob("channel_*.md"))
        }
        return cls(core=core, channels=channels, version=version)


def tenant_block(profile: TenantProfile, persona: str, policy: str) -> str:
    """Tenant persona and policy: only static hotel-level text, never guest data."""
    languages = ", ".join(profile.languages)
    lines = [
        "# HOTEL",
        f"Name: {profile.name}",
        f"Supported languages: {languages}. Default: {profile.default_language}.",
        f"Time zone: {profile.timezone}.",
    ]
    if profile.contacts.reception_phone:
        lines.append(f"Reception phone: {profile.contacts.reception_phone}")
    if profile.contacts.email:
        lines.append(f"Reception e-mail: {profile.contacts.email}")
    if profile.contacts.privacy_url:
        lines.append(f"Privacy notice: {profile.contacts.privacy_url}")
    staffed = "; ".join(f"{k}: {v}" for k, v in profile.staffed_hours.items())
    lines.append(f"Staffed hours: {staffed}.")
    sla = profile.handoff.sla
    lines.append(
        f"Handoff response targets (only when facts list handoff.sla): P1 {sla.p1_min} min, "
        f"P2 {sla.p2_min} min, P3 next business morning."
    )
    if persona.strip():
        lines += ["", "## Tone", persona.strip()]
    if policy.strip():
        lines += ["", "## Policy", policy.strip()]
    return "\n".join(lines)


class PromptAssembler:
    def __init__(self, texts: PromptTexts, canary: str) -> None:
        self.texts = texts
        self.canary = canary

    def system_blocks(
        self,
        profile: TenantProfile,
        *,
        kb_snapshot: str,
        channel: str,
        persona: str = "",
        policy: str = "",
    ) -> list[TextBlock]:
        core = self.texts.core.replace("{canary}", self.canary)
        channel_text = self.texts.channels.get(channel) or self.texts.channels.get("widget", "")
        return [
            TextBlock(core),
            TextBlock(tenant_block(profile, persona, policy)),
            TextBlock("# HOTEL FACTS (approved, public)\n" + kb_snapshot),
            TextBlock(channel_text, cache=True),
        ]

    @staticmethod
    def envelope(
        *,
        now: datetime,
        tz: str,
        reply_language: str,
        verified: bool,
        window_open: bool | None,
        channel: str,
        guest_kind: str = "unknown",
        extra: dict[str, str] | None = None,
    ) -> str:
        """The context envelope placed at the start of the newest user turn."""
        lines = [
            "[context]",
            f"hotel_local_datetime: {format_local(now, tz, 'en')} ({tz})",
            f"reply_language: {reply_language}",
            f"verified_reservation: {'yes' if verified else 'no'}",
            f"channel: {channel}",
            f"guest_kind: {guest_kind}",
        ]
        if window_open is not None:
            lines.append(f"service_window: {'open' if window_open else 'closed'}")
        for key, value in (extra or {}).items():
            lines.append(f"{key}: {value}")
        lines.append("[/context]")
        return "\n".join(lines)

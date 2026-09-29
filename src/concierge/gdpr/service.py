"""Data-subject requests (F25): search by blind index, export, erasure, restriction.

Implements: F25, F15, F26, C3, C15, C16

A subject is found by phone, e-mail or booking reference through the keyed blind
indexes, never by scanning plaintext. Export returns every stored item as JSON (and
a PDF); erasure deletes or scrubs across every guest-data table, keeps suppression
rows so the person is never messaged again, keeps consent proof (no content), and
writes a content-free deletion log. The registry below is what the coverage test
checks against ``GUEST_DATA_TABLES``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from concierge import audit
from concierge.clock import Clock
from concierge.db.base import Base
from concierge.db.models import (
    CallbackTask,
    Consent,
    Conversation,
    ConversationNote,
    DeletionLog,
    GdprRequest,
    Guest,
    GuestRequest,
    Handoff,
    KbGap,
    Meldeschein,
    Message,
    MessageText,
    Outbox,
    PaymentProof,
    QaReview,
    Reservation,
    Suppression,
    VoiceCall,
    WebhookEvent,
)
from concierge.db.session import ALLOW_APPEND_ONLY_DELETE, Database
from concierge.reports.pdf import simple_pdf
from concierge.security.crypto import Keyring

REQUEST_DEADLINE_DAYS = 30
SUBPROCESSOR_TASKS: tuple[dict[str, str], ...] = (
    {
        "id": "meta",
        "text": "Meta (WhatsApp): the hotel's business account keeps its own copies; request "
        "deletion in WhatsApp Manager (procedure VERIFY).",
    },
    {
        "id": "anthropic",
        "text": "Anthropic: API inputs are not used for training and are not retained beyond "
        "the request under the DPA (zero-retention VERIFY); nothing to delete.",
    },
    {
        "id": "retell",
        "text": "Retell (voice only): request deletion of transcripts and logs by e-mail to "
        "support (API VERIFY).",
    },
    {
        "id": "psp",
        "text": "PSP (registration only): the PaymentIntent was cancelled; ask the PSP to "
        "delete the session record if the guest asks (VERIFY).",
    },
    {"id": "backups", "text": "Backups: the subject's data leaves the backups by their age."},
)

# Which model each policy covers; the registry test proves GUEST_DATA_TABLES is covered.
EXPORTED: frozenset[type[Base]] = frozenset(
    {
        Guest,
        Conversation,
        Message,
        MessageText,
        Handoff,
        GuestRequest,
        ConversationNote,
        Reservation,
        Consent,
        Suppression,
        GdprRequest,
        Meldeschein,
        PaymentProof,
        VoiceCall,
        CallbackTask,
        QaReview,
        KbGap,
        WebhookEvent,
        Outbox,
    }
)
ERASED: frozenset[type[Base]] = frozenset(
    {
        Guest,
        Conversation,
        Message,
        MessageText,
        Handoff,
        GuestRequest,
        ConversationNote,
        Reservation,
        Meldeschein,
        PaymentProof,
        VoiceCall,
        CallbackTask,
        QaReview,
        KbGap,
        Outbox,
        WebhookEvent,
        GdprRequest,
    }
)
KEPT_BY_DESIGN: dict[type[Base], str] = {
    Suppression: "kept: proves the opt-out and prevents further messages (no content)",
    Consent: "kept for the consent's life plus the limitation period (subject hash only)",
}


@dataclass
class Subject:
    subject_kind: str
    subject_bidx: str
    guest_ids: list[str] = field(default_factory=list)
    reservation_ids: list[str] = field(default_factory=list)
    conversation_ids: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.guest_ids or self.reservation_ids)


class GdprError(ValueError):
    pass


class GdprService:
    def __init__(self, db: Database, clock: Clock, keyring: Keyring) -> None:
        self.db = db
        self.clock = clock
        self.keyring = keyring

    # -------------------------------------------------------------- consent
    async def record_consent(
        self,
        tenant_id: str,
        *,
        subject_bidx: str,
        kind: str,
        text_version: str,
        given_at: datetime,
        channel: str,
        guest_id: str | None = None,
        reservation_id: str | None = None,
        source_page: str | None = None,
        ip_ref: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> Consent | None:
        """Append one consent row per subject, kind and text version (no content stored)."""
        async with self.db.tenant_scope(tenant_id) as session:
            existing = await session.scalar(
                select(Consent).where(
                    Consent.tenant_id == tenant_id,
                    Consent.subject_bidx == subject_bidx,
                    Consent.kind == kind,
                    Consent.text_version == text_version,
                    Consent.withdrawn_at.is_(None),
                )
            )
            if existing is not None:
                return None
            row = Consent(
                tenant_id=tenant_id,
                guest_id=guest_id,
                reservation_id=reservation_id,
                subject_bidx=subject_bidx,
                kind=kind,
                text_version=text_version,
                given_at=given_at,
                channel=channel,
                source_page=source_page,
                ip_ref=ip_ref,
                evidence=evidence or {},
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def withdraw_consent(self, tenant_id: str, subject_bidx: str, kind: str) -> int:
        """Withdrawal is a new row (append-only): the same subject and kind, marked withdrawn."""
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            active = list(
                (
                    await session.execute(
                        select(Consent).where(
                            Consent.tenant_id == tenant_id,
                            Consent.subject_bidx == subject_bidx,
                            Consent.kind == kind,
                            Consent.withdrawn_at.is_(None),
                        )
                    )
                ).scalars()
            )
            if not active:
                return 0
            session.add(
                Consent(
                    tenant_id=tenant_id,
                    subject_bidx=subject_bidx,
                    kind=kind,
                    text_version=active[-1].text_version,
                    given_at=active[-1].given_at,
                    channel=active[-1].channel,
                    withdrawn_at=now,
                    evidence={"withdrawn": True},
                )
            )
            await session.commit()
            return len(active)

    # --------------------------------------------------------------- search
    async def search(
        self,
        tenant_id: str,
        *,
        phone: str | None = None,
        email: str | None = None,
        res_id: str | None = None,
    ) -> Subject:
        if phone:
            kind, value, column_guest, column_res = (
                "phone",
                phone,
                Guest.phone_bidx,
                Reservation.phone_bidx,
            )
        elif email:
            kind, value, column_guest, column_res = (
                "email",
                email,
                Guest.email_bidx,
                Reservation.email_bidx,
            )
        elif res_id:
            kind, value, column_guest, column_res = "res_id", res_id.strip(), None, None
        else:
            raise GdprError("a phone number, e-mail address or booking reference is required")
        subject = Subject(
            kind, "" if kind == "res_id" else self.keyring.blind_index(value, tenant_id, kind)
        )
        async with self.db.tenant_scope(tenant_id) as session:
            if kind == "res_id":
                reservations = list(
                    (
                        await session.execute(
                            select(Reservation).where(
                                Reservation.tenant_id == tenant_id,
                                Reservation.res_id == value,
                            )
                        )
                    ).scalars()
                )
                subject.subject_bidx = self.keyring.blind_index(value.lower(), tenant_id, "res_id")
            else:
                assert column_res is not None
                reservations = list(
                    (
                        await session.execute(
                            select(Reservation).where(
                                Reservation.tenant_id == tenant_id,
                                column_res == subject.subject_bidx,
                            )
                        )
                    ).scalars()
                )
            subject.reservation_ids = [r.id for r in reservations]
            guest_ids = {r.guest_id for r in reservations if r.guest_id}
            if column_guest is not None:
                guests = list(
                    (
                        await session.execute(
                            select(Guest).where(
                                Guest.tenant_id == tenant_id, column_guest == subject.subject_bidx
                            )
                        )
                    ).scalars()
                )
                guest_ids.update(g.id for g in guests)
            subject.guest_ids = sorted(guest_ids)
            if subject.guest_ids:
                conversations = list(
                    (
                        await session.execute(
                            select(Conversation.id).where(
                                Conversation.tenant_id == tenant_id,
                                Conversation.guest_id.in_(subject.guest_ids),
                            )
                        )
                    ).scalars()
                )
                subject.conversation_ids = sorted(conversations)
        return subject

    # ------------------------------------------------------------- requests
    async def create_request(
        self, tenant_id: str, kind: str, subject: Subject, *, actor: str, notes: str = ""
    ) -> GdprRequest:
        if kind not in {"export", "erasure", "restriction"}:
            raise GdprError(f"unknown request kind {kind!r}")
        now = self.clock.now()
        tasks: list[dict[str, Any]] = []
        if kind == "erasure":
            tasks = [{**task, "done": False} for task in SUBPROCESSOR_TASKS]
        async with self.db.tenant_scope(tenant_id) as session:
            row = GdprRequest(
                tenant_id=tenant_id,
                kind=kind,
                subject_bidx=subject.subject_bidx,
                subject_kind=subject.subject_kind,
                due_at=now + timedelta(days=REQUEST_DEADLINE_DAYS),
                tasks=tasks,
                notes=notes[:2000],
            )
            session.add(row)
            audit.record(
                session,
                actor=actor,
                action=f"gdpr.{kind}.requested",
                target=subject.subject_bidx[:16],
                tenant_id=tenant_id,
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def requests(self, tenant_id: str) -> list[GdprRequest]:
        async with self.db.tenant_scope(tenant_id) as session:
            return list(
                (
                    await session.execute(
                        select(GdprRequest)
                        .where(GdprRequest.tenant_id == tenant_id)
                        .order_by(GdprRequest.status, GdprRequest.due_at)
                    )
                ).scalars()
            )

    async def complete_task(
        self, tenant_id: str, request_id: str, task_id: str, *, actor: str
    ) -> GdprRequest:
        async with self.db.tenant_scope(tenant_id) as session:
            row = await session.get(GdprRequest, request_id)
            if row is None or row.tenant_id != tenant_id:
                raise GdprError("unknown request")
            tasks = [dict(t) for t in row.tasks]
            for task in tasks:
                if task.get("id") == task_id:
                    task["done"] = True
            row.tasks = tasks
            if all(t.get("done") for t in tasks) and row.status == "erased":
                row.status = "done"
                row.completed_at = self.clock.now()
            audit.record(
                session, actor=actor, action="gdpr.task_done", target=task_id, tenant_id=tenant_id
            )
            await session.commit()
            await session.refresh(row)
            return row

    # --------------------------------------------------------------- export
    async def export(self, tenant_id: str, subject: Subject) -> dict[str, Any]:
        """Every stored item about the subject, decrypted, as one JSON-ready mapping."""
        data: dict[str, Any] = {
            "subject": {"kind": subject.subject_kind, "reference": subject.subject_bidx},
            "generated_at": self.clock.now().isoformat(),
            "guests": [],
            "conversations": [],
            "reservations": [],
            "registration": [],
            "requests": [],
            "callbacks": [],
            "voice_calls": [],
            "consents": [],
            "suppressions": [],
            "data_requests": [],
            "handoffs": [],
            "reviews": [],
            "kb_gaps": [],
            "outbox": [],
            "webhook_events": [],
        }
        async with self.db.tenant_scope(tenant_id) as session:
            for guest_id in subject.guest_ids:
                guest = await session.get(Guest, guest_id)
                if guest is None:
                    continue
                data["guests"].append(
                    {
                        "id": guest.id,
                        "channel": guest.channel,
                        "name": self._dec(guest.name_enc, tenant_id, "name"),
                        "phone": self._dec(guest.phone_enc, tenant_id, "phone"),
                        "email": self._dec(guest.email_enc, tenant_id, "email"),
                        "language": guest.lang,
                        "restricted": guest.restricted,
                        "first_seen": guest.created_at.isoformat(),
                        "last_seen": guest.last_seen_at.isoformat() if guest.last_seen_at else None,
                    }
                )
            for conversation_id in subject.conversation_ids:
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None:
                    continue
                rows = (
                    await session.execute(
                        select(Message, MessageText)
                        .outerjoin(MessageText, MessageText.id == Message.id)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.occurred_at)
                    )
                ).all()
                data["conversations"].append(
                    {
                        "id": conversation.id,
                        "channel": conversation.channel,
                        "language": conversation.lang,
                        "status": conversation.status,
                        "started": conversation.created_at.isoformat(),
                        "messages": [
                            {
                                "direction": m.direction,
                                "at": m.occurred_at.isoformat(),
                                "kind": m.kind,
                                "text": t.text if t is not None else None,
                            }
                            for m, t in rows
                        ],
                        "notes": [
                            n.text
                            for n in (
                                await session.execute(
                                    select(ConversationNote).where(
                                        ConversationNote.conversation_id == conversation_id
                                    )
                                )
                            ).scalars()
                        ],
                    }
                )
                for handoff in (
                    await session.execute(
                        select(Handoff).where(Handoff.conversation_id == conversation_id)
                    )
                ).scalars():
                    data["handoffs"].append(
                        {
                            "conversation": conversation_id,
                            "priority": handoff.priority,
                            "status": handoff.status,
                            "reason": handoff.reason,
                            "created": handoff.created_at.isoformat(),
                        }
                    )
                for request in (
                    await session.execute(
                        select(GuestRequest).where(GuestRequest.conversation_id == conversation_id)
                    )
                ).scalars():
                    data["requests"].append(
                        {"kind": request.kind, "status": request.status, "details": request.details}
                    )
                for task in (
                    await session.execute(
                        select(CallbackTask).where(CallbackTask.conversation_id == conversation_id)
                    )
                ).scalars():
                    data["callbacks"].append(
                        {
                            "reason": task.reason,
                            "status": task.status,
                            "phone": self._dec(task.phone_enc, tenant_id, "phone"),
                        }
                    )
                for review in (
                    await session.execute(
                        select(QaReview).where(QaReview.conversation_id == conversation_id)
                    )
                ).scalars():
                    data["reviews"].append({"reason": review.reason, "status": review.status})
                for gap in (
                    await session.execute(
                        select(KbGap).where(KbGap.conversation_id == conversation_id)
                    )
                ).scalars():
                    data["kb_gaps"].append({"question": gap.question, "lang": gap.lang})
                for out in (
                    await session.execute(
                        select(Outbox).where(Outbox.conversation_id == conversation_id)
                    )
                ).scalars():
                    data["outbox"].append(
                        {"channel": out.channel, "state": out.state, "category": out.category}
                    )
                for call in (
                    await session.execute(
                        select(VoiceCall).where(VoiceCall.conversation_id == conversation_id)
                    )
                ).scalars():
                    data["voice_calls"].append(
                        {
                            "provider": call.provider,
                            "started": call.started_at.isoformat() if call.started_at else None,
                            "duration_s": call.duration_s,
                            "transcript": call.transcript,
                            "summary": call.summary_de,
                            "outcome": call.outcome,
                        }
                    )
            for reservation_id in subject.reservation_ids:
                reservation = await session.get(Reservation, reservation_id)
                if reservation is None:
                    continue
                data["reservations"].append(
                    {
                        "res_id": reservation.res_id,
                        "name": self._dec(reservation.name_enc, tenant_id, "name"),
                        "phone": self._dec(reservation.phone_enc, tenant_id, "phone"),
                        "email": self._dec(reservation.email_enc, tenant_id, "email"),
                        "arrival": reservation.arrival.isoformat(),
                        "departure": reservation.departure.isoformat(),
                        "status": reservation.status,
                        "opt_in": {
                            "whatsapp": reservation.whatsapp_optin,
                            "marketing": reservation.marketing_optin,
                            "source": reservation.optin_source,
                            "at": reservation.optin_at.isoformat()
                            if reservation.optin_at
                            else None,
                        },
                        "journey": reservation.meta.get("journey_log", []),
                    }
                )
                for form in (
                    await session.execute(
                        select(Meldeschein).where(Meldeschein.reservation_id == reservation_id)
                    )
                ).scalars():
                    proofs = list(
                        (
                            await session.execute(
                                select(PaymentProof).where(PaymentProof.meldeschein_id == form.id)
                            )
                        ).scalars()
                    )
                    data["registration"].append(
                        {
                            "arrival": form.arrival.isoformat(),
                            "confirmed": form.confirmed_at.isoformat()
                            if form.confirmed_at
                            else None,
                            "fields": json.loads(
                                self.keyring.decrypt_str(form.data_enc, tenant_id, "meldeschein")
                            ),
                            "proofs": [
                                {"provider": p.provider, "status": p.status} for p in proofs
                            ],
                        }
                    )
            consents = (
                await session.execute(
                    select(Consent).where(
                        Consent.tenant_id == tenant_id,
                        Consent.subject_bidx == subject.subject_bidx,
                    )
                )
            ).scalars()
            data["consents"] = [
                {
                    "kind": c.kind,
                    "text_version": c.text_version,
                    "given_at": c.given_at.isoformat(),
                    "channel": c.channel,
                    "withdrawn_at": c.withdrawn_at.isoformat() if c.withdrawn_at else None,
                }
                for c in consents
            ]
            suppressions = (
                await session.execute(
                    select(Suppression).where(
                        Suppression.tenant_id == tenant_id,
                        Suppression.subject_bidx == subject.subject_bidx,
                    )
                )
            ).scalars()
            data["suppressions"] = [
                {"channel": s.channel, "reason": s.reason, "since": s.created_at.isoformat()}
                for s in suppressions
            ]
            requests = (
                await session.execute(
                    select(GdprRequest).where(
                        GdprRequest.tenant_id == tenant_id,
                        GdprRequest.subject_bidx == subject.subject_bidx,
                    )
                )
            ).scalars()
            data["data_requests"] = [
                {"kind": r.kind, "status": r.status, "due": r.due_at.isoformat()} for r in requests
            ]
            needles = await self._payload_needles(session, tenant_id, subject)
            events = (
                await session.execute(
                    select(WebhookEvent).where(WebhookEvent.tenant_id == tenant_id)
                )
            ).scalars()
            data["webhook_events"] = [
                {"source": e.source, "received": e.received_at.isoformat()}
                for e in events
                if _mentions(e, needles)
            ]
        return data

    @staticmethod
    def export_pdf(data: dict[str, Any], *, hotel: str) -> bytes:
        sections: list[tuple[str, list[str]]] = []
        for key in (
            "guests",
            "reservations",
            "registration",
            "consents",
            "suppressions",
            "requests",
            "callbacks",
            "voice_calls",
            "handoffs",
            "data_requests",
        ):
            items = data.get(key) or []
            lines = [json.dumps(item, ensure_ascii=False, default=str)[:400] for item in items]
            sections.append((key, lines or ["-"]))
        for conversation in data.get("conversations") or []:
            lines = [
                f"{m['at']} {m['direction']}: {m['text'] or '[expired]'}"
                for m in conversation.get("messages", [])
            ]
            sections.append(
                (f"conversation {conversation['id']} ({conversation['channel']})", lines or ["-"])
            )
        return simple_pdf(
            f"Data export: {hotel}",
            sections,
            subtitle=f"Generated {data.get('generated_at', '')} (Art. 15 GDPR, draft wording)",
            footer="Guest Concierge data export; texts already expired by retention are marked.",
        )

    # -------------------------------------------------------------- erasure
    async def erase(
        self, tenant_id: str, subject: Subject, *, actor: str, request_id: str | None = None
    ) -> dict[str, int]:
        """Delete the subject's rows; suppressions and consent proof stay; log counts."""
        counts: dict[str, int] = {}
        now = self.clock.now()
        async with self.db.tenant_scope(tenant_id) as session:
            session.info[ALLOW_APPEND_ONLY_DELETE] = True

            async def delete_all(model: type[Any], *criteria: Any) -> None:
                rows: list[Any] = list(
                    (await session.execute(select(model).where(*criteria))).scalars()
                )
                for row in rows:
                    await session.delete(row)
                if rows:
                    name = str(model.__tablename__)
                    counts[name] = counts.get(name, 0) + len(rows)

            for conversation_id in subject.conversation_ids:
                await delete_all(
                    MessageText,
                    MessageText.id.in_(
                        select(Message.id).where(Message.conversation_id == conversation_id)
                    ),
                )
                await delete_all(Message, Message.conversation_id == conversation_id)
                await delete_all(
                    ConversationNote, ConversationNote.conversation_id == conversation_id
                )
                await delete_all(Handoff, Handoff.conversation_id == conversation_id)
                await delete_all(GuestRequest, GuestRequest.conversation_id == conversation_id)
                await delete_all(CallbackTask, CallbackTask.conversation_id == conversation_id)
                await delete_all(QaReview, QaReview.conversation_id == conversation_id)
                await delete_all(KbGap, KbGap.conversation_id == conversation_id)
                await delete_all(Outbox, Outbox.conversation_id == conversation_id)
                calls = list(
                    (
                        await session.execute(
                            select(VoiceCall).where(VoiceCall.conversation_id == conversation_id)
                        )
                    ).scalars()
                )
                for call in calls:
                    call.transcript = None
                    call.transcript_expires_at = None
                    call.summary_de = ""
                    call.caller_ref = "erased"
                    call.conversation_id = None
                if calls:
                    counts["voice_call"] = counts.get("voice_call", 0) + len(calls)
                await delete_all(Conversation, Conversation.id == conversation_id)
            for reservation_id in subject.reservation_ids:
                forms = list(
                    (
                        await session.execute(
                            select(Meldeschein).where(Meldeschein.reservation_id == reservation_id)
                        )
                    ).scalars()
                )
                for form in forms:
                    await delete_all(PaymentProof, PaymentProof.meldeschein_id == form.id)
                    await session.delete(form)
                if forms:
                    counts["meldeschein"] = counts.get("meldeschein", 0) + len(forms)
                await delete_all(Reservation, Reservation.id == reservation_id)
            needles = await self._payload_needles(session, tenant_id, subject)
            for guest_id in subject.guest_ids:
                await delete_all(Guest, Guest.id == guest_id)
            hooks = list(
                (
                    await session.execute(
                        select(WebhookEvent).where(WebhookEvent.tenant_id == tenant_id)
                    )
                ).scalars()
            )
            removed_hooks = 0
            for hook in hooks:
                if _mentions(hook, needles):
                    await session.delete(hook)
                    removed_hooks += 1
            if removed_hooks:
                counts["webhook_event"] = removed_hooks
            for table, count in counts.items():
                session.add(
                    DeletionLog(
                        tenant_id=tenant_id, table_name=table, row_count=count, reason="erasure"
                    )
                )
            if request_id:
                request = await session.get(GdprRequest, request_id)
                if request is not None and request.tenant_id == tenant_id:
                    request.status = "erased" if request.tasks else "done"
                    request.completed_at = now if request.status == "done" else None
                    request.export_ref = None
            audit.record(
                session,
                actor=actor,
                action="gdpr.erased",
                target=subject.subject_bidx[:16],
                tenant_id=tenant_id,
                details={"tables": counts},
            )
            await session.commit()
        return counts

    async def restrict(self, tenant_id: str, subject: Subject, on: bool, *, actor: str) -> int:
        async with self.db.tenant_scope(tenant_id) as session:
            changed = 0
            for guest_id in subject.guest_ids:
                guest = await session.get(Guest, guest_id)
                if guest is not None and guest.restricted != on:
                    guest.restricted = on
                    changed += 1
            audit.record(
                session,
                actor=actor,
                action="gdpr.restricted" if on else "gdpr.unrestricted",
                target=subject.subject_bidx[:16],
                tenant_id=tenant_id,
            )
            await session.commit()
        return changed

    def _dec(self, blob: bytes | None, tenant_id: str, name: str) -> str | None:
        if blob is None:
            return None
        return self.keyring.decrypt_str(blob, tenant_id, name)

    async def _payload_needles(self, session: Any, tenant_id: str, subject: Subject) -> list[str]:
        """Strings that identify the subject inside stored webhook payloads: internal ids
        plus the channel keys Meta uses (WhatsApp user id, phone digits)."""
        needles: set[str] = {*subject.guest_ids, *subject.reservation_ids}
        if subject.guest_ids:
            guests = (
                await session.execute(
                    select(Guest).where(
                        Guest.tenant_id == tenant_id, Guest.id.in_(subject.guest_ids)
                    )
                )
            ).scalars()
            for guest in guests:
                for key in (guest.wa_user_id, guest.channel_key):
                    if key:
                        needles.add(str(key))
                phone = self._dec(guest.phone_enc, tenant_id, "phone")
                if phone:
                    needles.add(phone.lstrip("+"))
        return [n for n in needles if len(n) >= 6]


def _mentions(event: WebhookEvent, needles: Sequence[str]) -> bool:
    """A stored webhook payload belongs to the subject when it contains one of the keys."""
    payload = getattr(event, "payload", None)
    if not payload or not needles:
        return False
    text = json.dumps(payload, ensure_ascii=False)
    return any(needle in text for needle in needles)
